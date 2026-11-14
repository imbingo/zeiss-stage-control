"""Optional Focus v2 and raw-snap capabilities for the ZEN adapter."""

from math import isclose, isfinite
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic

from zen_images import RawFrame


class MarkCapabilityError(RuntimeError):
    """A required mark-capture service is not exposed by this device."""


class GatewayMarkMixin:
    def _mark_stub(self, module, service):
        from importlib import import_module
        if self._channel is None:
            raise RuntimeError("请先连接 Gateway")
        return getattr(import_module(module), service)(self._channel)

    _V1_AF_SERVICE = 'zen_api.lm.acquisition.v1.ExperimentSwAutofocusService'

    def _dyn_pool(self, service_name):
        """Descriptor pool for `service_name`, built from reflection.

        The Gateway serves the exact FileDescriptor it registered, which is
        the only trustworthy schema when the installed ZEN registers the
        service under a different package than our generated stubs (e.g. the
        v1beta variants on ZEN 3.12). Returns None when the schema cannot be
        fetched or parsed; the result (pool or None) is cached per service.
        """
        pools = getattr(self, '_dyn_pools', None)
        if pools is None:
            pools = self._dyn_pools = {}
        if service_name not in pools:
            pools[service_name] = self._build_dyn_pool(service_name)
        return pools[service_name]

    def _build_dyn_pool(self, service_name):
        try:
            from .reflection import file_descriptor_protos
            from google.protobuf import descriptor_pool
            from google.protobuf import wrappers_pb2, empty_pb2, struct_pb2, timestamp_pb2, duration_pb2
            builtins = {}
            for module in (wrappers_pb2, empty_pb2, struct_pb2, timestamp_pb2, duration_pb2):
                builtins[module.DESCRIPTOR.name] = module.DESCRIPTOR.serialized_pb
            protos = file_descriptor_protos(self._channel, service_name, self._timeout)
            pool = descriptor_pool.DescriptorPool()
            added = set(builtins)
            for raw in builtins.values():
                pool.AddSerializedFile(raw)
            # The response embeds transitive deps incl. well-known types that
            # are already in the pool; adding them again raises TypeError.
            pending = {proto.name: proto for proto in protos if proto.name not in added}
            while pending:
                progress = False
                for name, proto in list(pending.items()):
                    if set(proto.dependency) - added:
                        continue
                    pool.Add(proto)
                    added.add(name)
                    del pending[name]
                    progress = True
                if not progress:
                    return None  # unresolved dependency chain
            return pool
        except Exception:
            return None

    # Get-style responses name the mode field `auto_focus_mode` while the
    # matching Set request calls it `autofocus_mode`; the template echo maps it.
    _FIELD_ALIASES = {'auto_focus_mode': 'autofocus_mode'}

    @staticmethod
    def _copy_fields(source, target, strict=True):
        """Copy set fields of `source` into `target` by name, recursively.

        Field-NAME copying tolerates wire field-number drift between schema
        versions. In strict mode (the authoritative request overlay), a field
        absent from the target schema fails the whole copy so a partial
        request is never sent. In lenient mode (template seeding from a
        prior response), unknown or type-mismatched fields are simply
        skipped: they are best-effort echoes, never the intended change.
        """
        aliases = GatewayMarkMixin._FIELD_ALIASES
        for field, value in source.ListFields():
            name = field.name
            target_desc = target.DESCRIPTOR.fields_by_name.get(name) or \
                target.DESCRIPTOR.fields_by_name.get(aliases.get(name, ''))
            if target_desc is None:
                if strict:
                    return False
                continue
            name = target_desc.name
            try:
                if field.is_repeated:
                    if field.message_type is not None:
                        if strict:
                            return False  # repeated messages/maps: bail out
                        continue
                    getattr(target, name).extend(value)
                    continue
                target_field = getattr(target, name)
                if field.message_type is not None:
                    # A single-'value' message is a scalar wrapper: copy the
                    # inner value directly. Recursing via ListFields() would
                    # silently drop default inner values (False/0) and leave a
                    # template-seeded True in place.
                    wrapper = len(value.DESCRIPTOR.fields) == 1 and \
                        value.DESCRIPTOR.fields[0].name == 'value'
                    if wrapper and target_desc.message_type is None:
                        setattr(target, name, value.value)  # wrapper -> scalar
                    elif wrapper and target_field.DESCRIPTOR.fields_by_name.get('value') is not None:
                        target_field.value = value.value    # wrapper -> wrapper
                    elif hasattr(target_field, 'ListFields') and not wrapper:
                        if not GatewayMarkMixin._copy_fields(value, target_field, strict):
                            if strict:
                                return False
                    elif strict:
                        return False
                elif target_desc.message_type is not None:
                    if target_field.DESCRIPTOR.fields_by_name.get('value') is None:
                        if strict:
                            return False
                    else:
                        target_field.value = value  # scalar -> wrapper
                else:
                    setattr(target, name, value)
            except (TypeError, ValueError):
                if strict:
                    raise
        return True

    def _dyn_call(self, service_name, method_name, request, timeout, template=None):
        """Call `service_name.method_name` via reflection-built messages.

        `template` is an optional earlier response (e.g. the Get reply whose
        parameters a Set request should preserve): its fields seed the
        request leniently before `request` fields are overlaid strictly.
        """
        pool = self._dyn_pool(service_name)
        if pool is None:
            return None
        from google.protobuf import message_factory
        try:
            service = pool.FindServiceByName(service_name)
            method = service.methods_by_name[method_name]
            request_class = message_factory.GetMessageClass(method.input_type)
            response_class = message_factory.GetMessageClass(method.output_type)
            dyn_request = request_class()
            if template is not None:
                self._copy_fields(template, dyn_request, strict=False)
            if not self._copy_fields(request, dyn_request):
                return None
        except Exception:
            return None
        call = self._channel.unary_unary(
            '/' + service_name + '/' + method_name,
            request_serializer=lambda message: message.SerializeToString(),
            response_deserializer=response_class.FromString,
        )
        return self._rpc(service_name + '.' + method_name, call, dyn_request, timeout=timeout)

    def _service_variants(self, service_name):
        """Registered Gateway services sharing the short name of `service_name`.

        ZEN 3.12 registers several lm.* services under v1beta packages while
        newer ZEN uses the names in our generated stubs. The reflection
        listing (cached in the capability matrix by probe_services) tells us
        exactly which variants exist on this machine.
        """
        short = service_name.rpartition('.')[2]
        names = getattr(self.capabilities, 'services', None)
        if not names:
            try:
                from .reflection import list_services
                names = list_services(self._channel, timeout=self._timeout)
            except Exception:
                names = ()
        family=service_name.rsplit('.',2)[0]
        return [name for name in names
                if name != service_name and name.endswith('.' + short)
                and name.rsplit('.',2)[0] == family]

    def _versioned_rpc(self, service_name, method_name, request, timeout, primary_call, template=None):
        """Call `service.method`, retrying on registered package variants.

        On UNIMPLEMENTED from the primary path, every other registered service
        with the same short name is tried via reflection-built dynamic
        messages. The rpc trace always records the path actually used.
        """
        import grpc
        self._last_service_path = service_name
        try:
            return self._rpc(service_name + '.' + method_name, primary_call,
                             request, timeout=timeout)
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
            original = error
        for variant in self._service_variants(service_name):
            try:
                reply = self._dyn_call(variant, method_name, request, timeout, template=template)
            except grpc.RpcError as error:
                if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                    raise
                continue
            if reply is not None:
                self._last_service_path = variant
                return reply
        raise original

    def _af_rpc(self, method_name, request, timeout, template=None):
        """AF method call: official v1 path first, then registered variants
        (ZEN 3.12 exposes the same service under the v1beta package)."""
        reply = self._versioned_rpc(self._V1_AF_SERVICE, method_name, request, timeout,
                                    getattr(self._af, method_name), template=template)
        self._last_af_path = self._last_service_path
        return reply

    def _focus_v1(self):
        """Focus v1 stub pair, or None when the generated stubs lack it.

        v1 reports MICROMETRES where v2 reports metres. Callers must convert
        and only reach this path via the explicit allow_focus_v1 opt-in.
        """
        try:
            from importlib import import_module
            pb2 = import_module('zen_api.lm.hardware.v1.focus_service_pb2')
            pb2_grpc = import_module('zen_api.lm.hardware.v1.focus_service_pb2_grpc')
            return pb2_grpc.FocusServiceStub(self._channel), pb2
        except ImportError:
            return None

    def _definite_focus(self):
        """Lazy DefiniteFocusService stub pair, or None when stubs are absent."""
        from importlib import import_module
        try:
            pb2 = import_module('zen_api.lm.acquisition.v1.definite_focus_service_pb2')
            pb2_grpc = import_module('zen_api.lm.acquisition.v1.definite_focus_service_pb2_grpc')
            return pb2_grpc.DefiniteFocusServiceStub(self._channel), pb2
        except ImportError:
            return None

    def definite_focus_surface(self, timeout_s):
        """Hardware Definite Focus FindSurface; returns Z in METRES.

        A different engine from software contrast AF: an infrared-reflection
        surface detector bounded by the ZEN-side Definite Focus configuration.
        Only reached when allow_definite_focus is enabled and the software AF
        service is absent.
        """
        import grpc
        from . import AutofocusTimeout, DeviceDisconnected
        pair = self._definite_focus()
        if pair is None:
            raise MarkCapabilityError("当前安装包未包含 DefiniteFocusService 存根，无法走硬件 AF")
        stub, pb2 = pair
        try:
            reply = self._rpc("zen_api.lm.acquisition.v1.DefiniteFocusService.FindSurface",
                              stub.FindSurface, pb2.DefiniteFocusServiceFindSurfaceRequest(),
                              timeout=timeout_s+self._timeout)
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise AutofocusTimeout("Definite Focus 找表面超时，设备状态待确认") from error
            if error.code() == grpc.StatusCode.UNAVAILABLE:
                raise DeviceDisconnected("Definite Focus 期间 Gateway 不可用，设备状态待确认") from error
            raise
        value = reply.zposition
        if not isfinite(value):
            raise ValueError("Definite Focus 返回无效 Z")
        return value

    def read_z(self):
        import grpc
        from zen_api.lm.hardware.v2.focus_service_pb2 import FocusServiceGetPositionRequest
        stub = self._mark_stub('zen_api.lm.hardware.v2.focus_service_pb2_grpc', 'FocusServiceStub')
        try:
            value = self._rpc("zen_api.lm.hardware.v2.FocusService.GetPosition", stub.GetPosition, FocusServiceGetPositionRequest(), timeout=self._timeout).value
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.UNIMPLEMENTED:
                if getattr(self, 'allow_focus_v1', False):
                    return self._read_z_v1()
                raise MarkCapabilityError(
                    str(error) + "；不会静默回退到单位为微米的 Focus v1"
                    "（如确认本机仅有 v1，可在连接设置中显式启用 ZEN_ALLOW_FOCUS_V1）") from error
            raise
        if not isfinite(value):
            raise ValueError("Focus v2 返回无效 Z")
        return value  # v2 is METRES. v1 micrometres require the explicit opt-in above.

    def _read_z_v1(self):
        pair = self._focus_v1()
        if pair is None:
            raise MarkCapabilityError("Focus v2 不可用，且当前安装包未包含 Focus v1 存根，无法降级")
        stub, pb2 = pair
        value = self._rpc("zen_api.lm.hardware.v1.FocusService.GetPosition", stub.GetPosition,
                          pb2.FocusServiceGetPositionRequest(), timeout=self._timeout).value
        if not isfinite(value):
            raise ValueError("Focus v1 返回无效 Z")
        return value * 1e-6  # v1 MICROMETRES -> metres

    def read_optics(self):
        from zen_api.lm.hardware.v2.objective_changer_service_pb2 import ObjectiveChangerServiceGetPositionRequest
        from zen_api.lm.hardware.v2.optovar_service_pb2 import OptovarServiceGetPositionRequest
        import grpc
        values = []
        for module, service, request in (
            ('objective_changer_service', 'ObjectiveChangerServiceStub', ObjectiveChangerServiceGetPositionRequest),
            ('optovar_service', 'OptovarServiceStub', OptovarServiceGetPositionRequest),
        ):
            stub = self._mark_stub('zen_api.lm.hardware.v2.'+module+'_pb2_grpc', service)
            try:
                values.append(self._rpc("zen_api.lm.hardware.v2."+service.removesuffix("Stub")+".GetPosition",
                                        stub.GetPosition, request(), timeout=self._timeout).value)
            except grpc.RpcError as error:
                if error.code() == grpc.StatusCode.UNIMPLEMENTED:
                    # Manual turret/optics can coexist with a motorized stage:
                    # an unregistered service means "cannot verify", whatever
                    # the XY mode. Positions stay None = explicitly unverified.
                    values.append(None)
                else:
                    raise
        return tuple(values)

    def move_z(self, z_m):
        import grpc
        from zen_api.lm.hardware.v2.focus_service_pb2 import FocusServiceMoveToRequest
        if not isfinite(z_m):
            raise ValueError("Z 目标无效")
        stub = self._mark_stub('zen_api.lm.hardware.v2.focus_service_pb2_grpc', 'FocusServiceStub')
        try:
            self._rpc('zen_api.lm.hardware.v2.FocusService.MoveTo', stub.MoveTo,
                      FocusServiceMoveToRequest(value=z_m), timeout=self._timeout)
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.UNIMPLEMENTED or not getattr(self, 'allow_focus_v1', False):
                raise
            pair = self._focus_v1()
            if pair is None:
                raise MarkCapabilityError("Focus v2 不可用，且当前安装包未包含 Focus v1 存根，无法降级") from error
            v1_stub, pb2 = pair
            self._rpc('zen_api.lm.hardware.v1.FocusService.MoveTo', v1_stub.MoveTo,
                      pb2.FocusServiceMoveToRequest(value=z_m * 1e6), timeout=self._timeout)  # metres -> µm

    def prepare_mark_experiment(self, name, low_m, high_m, *, use_autofocus=True):
        from google.protobuf.wrappers_pb2 import BoolValue, DoubleValue
        from zen_api.acquisition.v1beta.experiment_service_pb2 import ExperimentServiceCloneRequest
        from zen_api.lm.acquisition.v1.experiment_sw_autofocus_service_pb2 import (
            ExperimentSwAutofocusServiceGetAutofocusParametersRequest as Get,
            ExperimentSwAutofocusServiceSetAutofocusParametersRequest as Set,
        )
        if not all(map(isfinite, (low_m, high_m))) or low_m >= high_m:
            raise ValueError("AF 范围无效")
        self.load_experiment(name)
        clone = self._rpc("zen_api.acquisition.v1beta.ExperimentService.Clone", self._experiments.Clone, ExperimentServiceCloneRequest(experiment_id=self._experiment_id), timeout=self._timeout)
        if not clone.experiment_id or clone.experiment_id == self._experiment_id:
            raise ValueError("未能取得独立的临时实验，拒绝修改原实验")
        self._experiment_id = clone.experiment_id
        if not use_autofocus:
            return {"experiment_id": self._experiment_id, "autofocus": False}
        import grpc
        read_error = None
        try:
            params = self._af_rpc('GetAutofocusParameters',
                                  Get(experiment_id=self._experiment_id), self._timeout)
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
            params = None
            read_error = error
        if params is None:
            df_method = 'zen_api.lm.acquisition.v1.DefiniteFocusService.FindSurface'
            if (getattr(self, 'allow_definite_focus', False) and self._definite_focus() is not None
                    and not self.capabilities.unsupported(df_method)):
                self._prepared_af = (self._experiment_id, 'definite_focus')
                return {'experiment_id': self._experiment_id, 'af_engine': 'definite_focus'}
            if not getattr(self, 'allow_unverified_af', False):
                raise MarkCapabilityError(
                    'AF 参数读取不可用，无法回读校验搜索范围；默认已阻止 AF。'
                    '请关闭 AF 改用手动示教；只有在 ZEN 核对范围安全后，才可显式启用允许实验内置 AF 范围。'
                ) from read_error
            # Get unavailable (seen on ZEN 3.12): constrain the search range
            # with a write-only Set. Unset wrapped/enumerated fields stay
            # unmodified on the experiment; mode/contrast are left untouched.
            try:
                self._af_rpc('SetAutofocusParameters', Set(
                    experiment_id=self._experiment_id,
                    relative_range_is_automatic=BoolValue(value=False),
                    relative_search_range=DoubleValue(value=high_m-low_m),
                    lower_limit=DoubleValue(value=low_m), upper_limit=DoubleValue(value=high_m),
                ), timeout=self._timeout)
            except grpc.RpcError as error:
                if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                    raise
                # Neither read nor write interface exists: the whole software
                # AF service is absent (ZEN 3.12). Prefer the hardware
                # Definite Focus engine when explicitly enabled; else the
                # experiment's stored range behind its own opt-in.
                df_method = "zen_api.lm.acquisition.v1.DefiniteFocusService.FindSurface"
                if getattr(self, 'allow_definite_focus', False) and self._definite_focus() is not None \
                        and not self.capabilities.unsupported(df_method):
                    return {"experiment_id": self._experiment_id, "af_engine": "definite_focus"}
                if not getattr(self, 'allow_unverified_af', False):
                    raise MarkCapabilityError(
                        str(error) + "；软件 AF 参数读写接口均不可用，无法限定搜索范围。"
                        "若本机装有 Definite Focus 硬件，可在连接设置启用“使用 Definite Focus 硬件对焦”；"
                        "或在 ZEN 中确认实验 AF 范围安全后启用“允许实验内置 AF 范围”，"
                        "或关闭自动对焦改用无 AF 手动示教") from error
                return {"experiment_id": self._experiment_id, "af_range": "experiment_stored_unverified"}
            return {"experiment_id": self._experiment_id, "af_range": "written_unverified_readback",
                    "lower_m": low_m, "upper_m": high_m}
        if params.auto_focus_mode not in (1, 2, 3):
            raise ValueError("采图实验需使用软件对比度／强度 AF，不支持反射式 AF")
        scalar = lambda name: getattr(getattr(params, name, None), 'value',
                                      getattr(params, name, None))
        values = dict(experiment_id=self._experiment_id, autofocus_mode=params.auto_focus_mode,
                contrast_measure=params.contrast_measure, search_strategy="Full",
                autofocus_sampling=params.autofocus_sampling,
                relative_range_is_automatic=BoolValue(value=False),
                relative_search_range=DoubleValue(value=high_m-low_m),
                lower_limit=DoubleValue(value=low_m), upper_limit=DoubleValue(value=high_m))
        for name, wrapper in (('offset', DoubleValue), ('use_acquisition_roi', BoolValue)):
            value = scalar(name)
            if value is not None:
                values[name] = wrapper(value=value)
        if scalar('reference_channel_name'):
            values['reference_channel_name'] = scalar('reference_channel_name')
        try:
            self._af_rpc('SetAutofocusParameters', Set(**values), self._timeout, template=params)
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                raise
            # Read works but write is missing: stored range is unverified too.
            if not getattr(self, 'allow_unverified_af', False):
                raise MarkCapabilityError(
                    str(error) + "；AF 参数写入接口不可用，无法限定搜索范围。"
                    "请在 ZEN 中确认实验 AF 范围安全后在连接设置启用“允许实验内置 AF 范围”，"
                    "或关闭自动对焦改用无 AF 手动示教") from error
            return {"experiment_id": self._experiment_id, "af_range": "experiment_stored_unverified"}
        actual = self._af_rpc('GetAutofocusParameters',
                              Get(experiment_id=self._experiment_id), self._timeout)
        if (actual.relative_range_is_automatic or actual.search_strategy.casefold() != 'full'
                or not isclose(actual.lower_limit, low_m, rel_tol=0, abs_tol=1e-12)
                or not isclose(actual.upper_limit, high_m, rel_tol=0, abs_tol=1e-12)
                or not isclose(actual.relative_search_range, high_m-low_m, rel_tol=0, abs_tol=1e-12)):
            raise ValueError("设备未接受所选层的限定 AF 范围，已阻止 Z 移动")
        return {"experiment_id": self._experiment_id, "lower_m": actual.lower_limit,
                "upper_m": actual.upper_limit, "mode": actual.auto_focus_mode,
                "search_strategy": actual.search_strategy}

    def _collapse_zstack(self, experiment_id):
        """Ensure the capture clone yields exactly one slice at the stage's
        current Z.

        RunSnap/RunExperiment honour the experiment's enabled dimensions: on
        a Z-stack experiment they sweep Z through the stored stack (starting
        at first_slice) and emit one frame per slice — wrong Z and ambiguous
        multi-frame data. There is no "disable dimension" RPC, so collapse
        the throwaway clone's stack to a single slice at the current Z
        (first == last). A ZStackService that is entirely absent cannot
        verify the dimension either way; the single-frame check downstream
        stays the last guard in that case.
        """
        import grpc
        from google.protobuf.wrappers_pb2 import DoubleValue
        try:
            from zen_api.lm.acquisition.v1.zstack_service_pb2 import (
                ZStackServiceIsZStackExperimentRequest as IsStack,
                ZStackServiceGetZStackInfoRequest as StackInfo,
                ZStackServiceModifyZStackFirstLastRequest as ModifyFirstLast,
                ZStackServiceModifyZStackCenterRangeRequest as ModifyCenterRange,
            )
            stub = self._mark_stub('zen_api.lm.acquisition.v1.zstack_service_pb2_grpc',
                                   'ZStackServiceStub')
        except (ImportError, AttributeError):
            return
        service = 'zen_api.lm.acquisition.v1.ZStackService'
        try:
            enabled = self._versioned_rpc(service, 'IsZStackExperiment',
                                          IsStack(experiment_id=experiment_id),
                                          self._timeout, stub.IsZStackExperiment).is_zstack_experiment
        except grpc.RpcError as error:
            if error.code() == grpc.StatusCode.UNIMPLEMENTED:
                return
            raise
        if not enabled:
            return
        info = self._versioned_rpc(service, 'GetZStackInfo',
                                   StackInfo(experiment_id=experiment_id),
                                   self._timeout, stub.GetZStackInfo)
        z_now = self.read_z()
        interval = info.interval if info.interval > 0 else 1e-6
        try:
            self._versioned_rpc(service, 'ModifyZStackFirstLast', ModifyFirstLast(
                experiment_id=experiment_id, first=DoubleValue(value=z_now),
                last=DoubleValue(value=z_now), interval=DoubleValue(value=interval)),
                self._timeout, stub.ModifyZStackFirstLast)
        except grpc.RpcError as error:
            if error.code() not in (grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.INVALID_ARGUMENT):
                raise  # never issue another write after timeout/transport/auth failure
            try:
                self._versioned_rpc(service, 'ModifyZStackCenterRange', ModifyCenterRange(
                    experiment_id=experiment_id, center=DoubleValue(value=z_now),
                    range=DoubleValue(value=0.0), interval=DoubleValue(value=interval)),
                    self._timeout, stub.ModifyZStackCenterRange)
            except grpc.RpcError:
                raise MarkCapabilityError(
                    "拍摄实验启用了 Z-stack 维度：RunSnap 会把 Z 移动到堆栈首层并逐层曝光。"
                    "已尝试把克隆实验收敛为当前 Z 的单层但被服务端拒绝（"
                    + str(error) + "）。请在 ZEN 中关闭该实验的 Z 维度，或改用一个单层实验") from error
        check = self._versioned_rpc(service, 'GetZStackInfo',
                                    StackInfo(experiment_id=experiment_id),
                                    self._timeout, stub.GetZStackInfo)
        if check.num_slices != 1:
            raise MarkCapabilityError(
                f"拍摄实验启用了 Z-stack 维度且收敛后仍为 {check.num_slices} 层；"
                "请在 ZEN 中关闭该实验的 Z 维度，或改用单层实验，已阻止拍摄")

    def capture_frame(self, timeout_s, stop, output_name, *, kind="camera_snap"):
        import grpc
        from google.protobuf.wrappers_pb2 import Int32Value
        from zen_api.acquisition.v1beta.experiment_service_pb2 import (
            ExperimentServiceRunSnapRequest, ExperimentServiceRunExperimentRequest,
            ExperimentServiceGetStatusRequest, ExperimentServiceStopRequest, ExperimentServiceCloneRequest,
        )
        from zen_api.acquisition.v1beta.experiment_streaming_service_pb2 import ExperimentStreamingServiceMonitorExperimentRequest

        if kind not in ("camera_snap", "confocal"):
            raise ValueError("未知的采集方式")
        if not self._experiment_id:
            raise RuntimeError("请先准备临时拍摄实验")
        # Use another clone exclusively for the exposure: the AF experiment's
        # final/buffered frame must never be accepted as the requested photograph.
        af_id = self._experiment_id
        capture = self._rpc("zen_api.acquisition.v1beta.ExperimentService.Clone", self._experiments.Clone, ExperimentServiceCloneRequest(experiment_id=af_id), timeout=self._timeout)
        if not capture.experiment_id or capture.experiment_id == af_id:
            raise ValueError("未能创建独立拍摄实验，拒绝接收可能属于 AF 的旧帧")
        experiment_id = self._experiment_id = capture.experiment_id
        self._collapse_zstack(experiment_id)
        streaming = self._mark_stub('zen_api.acquisition.v1beta.experiment_streaming_service_pb2_grpc', 'ExperimentStreamingServiceStub')
        # Subscribe BEFORE triggering this single exposure. A unique clone ID
        # prevents a live/previous/AF frame from being mistaken for this snap.
        try:
            stream = streaming.MonitorExperiment(ExperimentStreamingServiceMonitorExperimentRequest(
                experiment_id=experiment_id, channel_index=Int32Value(value=0), enable_raw_data=False,
            ), timeout=timeout_s)
        except grpc.RpcError as error:
            raise self._rpc_failure("zen_api.acquisition.v1beta.ExperimentStreamingService.MonitorExperiment", error) from error
        messages, started, closing = Queue(), Event(), Event()
        def receive():
            started.set()
            try:
                for reply in stream:
                    if reply.frame_data.experiment_id == experiment_id:
                        messages.put(reply.frame_data)
            except Exception as error:
                if closing.is_set() and isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.CANCELLED:
                    return
                messages.put(self._rpc_failure("zen_api.acquisition.v1beta.ExperimentStreamingService.MonitorExperiment", error)
                             if isinstance(error, grpc.RpcError) else error)
        reader = Thread(target=receive, daemon=True)
        reader.start()
        started.wait()
        snap, frame = None, None
        capture_rpc_done = False
        deadline = monotonic() + timeout_s
        try:
            if stop.is_set():
                raise InterruptedError("已请求停止拍摄")
            # Confocal must execute the saved acquisition experiment. Never fall
            # back to the camera/localisation Snap endpoint on an error.
            method = self._experiments.RunExperiment if kind == 'confocal' else self._experiments.RunSnap
            request = ExperimentServiceRunExperimentRequest if kind == 'confocal' else ExperimentServiceRunSnapRequest
            capture_method = "zen_api.acquisition.v1beta.ExperimentService." + ("RunExperiment" if kind == "confocal" else "RunSnap")
            capabilities = getattr(self, 'capabilities', None)
            if capabilities is not None and capabilities.unsupported(capture_method):
                self._rpc_record(capture_method, 'UNIMPLEMENTED_CACHED')
                from .rpc_diagnostics import GatewayCallError
                raise GatewayCallError(capture_method, grpc.StatusCode.UNIMPLEMENTED)
            try:
                snap = method.future(request(experiment_id=experiment_id, output_name=output_name), timeout=timeout_s)
            except grpc.RpcError as error:
                raise self._rpc_failure(capture_method, error) from error
            while True:
                if stop.is_set():
                    raise InterruptedError("已请求停止拍摄")
                if monotonic() >= deadline:
                    raise TimeoutError("单张拍摄或完整图像接收超时，不会自动重拍")
                try:
                    item = messages.get(timeout=.02)
                except Empty:
                    item = None
                if isinstance(item, Exception):
                    # Some servers end the stream with the snap. A received full
                    # frame may still be valid, but the snap RPC must also succeed.
                    if frame is None:
                        raise item
                elif item is not None:
                    if frame is not None:
                        raise ValueError("一次单张拍摄返回多帧，拒绝不明确的图像")
                    frame = item
                if snap.done() and not capture_rpc_done:
                    try:
                        snap.result()
                    except grpc.RpcError as error:
                        raise self._rpc_failure(capture_method, error) from error
                    self._rpc_record(capture_method, "OK")
                    capture_rpc_done = True
                if capture_rpc_done and frame is not None:
                    break
            if kind == 'confocal':
                while True:
                    if stop.is_set():
                        raise InterruptedError("已请求停止共聚焦采集")
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError("共聚焦实验结束状态未确认，未保存图像")
                    status = self._rpc("zen_api.acquisition.v1beta.ExperimentService.GetStatus", self._experiments.GetStatus, ExperimentServiceGetStatusRequest(
                        experiment_id=experiment_id), timeout=min(self._timeout, remaining)).status
                    counts = (status.images_count, status.channels_count, status.zstack_slices_count,
                              status.time_points_count, status.scenes_count, status.cumulated_tiles_count, status.tiles_count)
                    if any(value > 1 for value in counts):
                        raise ValueError("共聚焦实验必须是单通道、单 Z 层、单时间点、无拼图的一张图像")
                    if not status.is_experiment_running and not status.is_acquisition_running and status.images_count == 1:
                        break
                    stop.wait(.05)
            p, f = frame.pixel_data, frame.frame_position
            if (p.start_position.x or p.start_position.y
                    or (p.size.width, p.size.height) != (frame.frame_size.width, frame.frame_size.height)
                    or f.c != 0 or any((f.z, f.t, f.s, f.m, f.h))):
                raise ValueError("需要单通道单帧的完整图像；收到部分帧或多维采集")
            position = frame.frame_stage_position
            if self.xy_enabled and not all(position.HasField(key) for key in ('x', 'y', 'z')):
                raise ValueError("原始图像未提供完整的 XYZ 位置")
            z_source = 'frame' if position.HasField('z') else 'focus_readback_after_capture'
            z = position.z.value if position.HasField('z') else self.read_z()
            xy = (position.x.value, position.y.value) if self.xy_enabled else (None, None)
            result = RawFrame(frame.frame_size.width, frame.frame_size.height, p.pixel_type,
                              p.raw_data, frame.scaling.x, frame.scaling.y, (*xy, z),
                              experiment_id, frame.frame_number, (f.x, f.y), f.c,
                              self.xy_enabled, z_source, kind)
            result.array()
            self._rpc_record("zen_api.acquisition.v1beta.ExperimentStreamingService.MonitorExperiment", "FRAME_RECEIVED")
            return result
        except BaseException as error:
            if snap is not None:
                snap.cancel()
                try:
                    self._rpc("zen_api.acquisition.v1beta.ExperimentService.Stop", self._experiments.Stop, ExperimentServiceStopRequest(experiment_id=experiment_id), timeout=self._timeout)
                except Exception:
                    pass  # Recovery remains locked even when Stop is accepted.
            if isinstance(error, grpc.RpcError) and error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise TimeoutError("单张拍摄或完整图像接收超时，不会自动重拍") from error
            raise
        finally:
            closing.set()
            stream.cancel()
            reader.join(timeout=1)

    def capture_stack(self,first,last,interval,timeout,stop,output_name):
        from .stack_capture import capture_stack
        return capture_stack(self,first,last,interval,timeout,stop,output_name)

    def inspect_mark_experiments(self, names):
        """Read names and positions only: NEVER Load/Clone/Export an experiment."""
        from zen_api.acquisition.v1beta.experiment_service_pb2 import ExperimentServiceGetAvailableExperimentsRequest
        from .rpc_diagnostics import diagnostic_from_error
        report = {"kind": "read_only_experiment_check", "device_actions_performed": False,
                  "available_experiments": None, "requested_experiments": [], "errors": [],
                  "limitations": ["列表接口只提供实验名，未加载实验，不能审查曝光/转盘/位置恢复设置",
                                  "未调用 Load、Clone、AF、MoveTo 或采集接口，不能确认它们在现场是否支持",
                                  "Z 读取成功不代表转盘可读或已锁定；只读检查通过不授权拍摄"]}
        try:
            available = self._rpc("zen_api.acquisition.v1beta.ExperimentService.GetAvailableExperiments",
                                  self._experiments.GetAvailableExperiments,
                                  ExperimentServiceGetAvailableExperimentsRequest(), timeout=self._timeout)
            report["available_experiments"] = [item.name for item in available.experiments]
        except Exception as error:
            report["errors"].append(diagnostic_from_error(error) or {"message": "实验列表读取失败"})
        for name in dict.fromkeys(name.strip() for name in names if name.strip()):
            exists = None if report["available_experiments"] is None else name in report["available_experiments"]
            report["requested_experiments"].append({"name": name, "exists": exists})
        for key, action in (("z_m", self.read_z), ("optics_positions", self.read_optics)):
            try:
                report[key] = action()
            except Exception as error:
                report[key] = None
                report["errors"].append(diagnostic_from_error(error) or {"message": key+" 读取失败"})
        # Distinguish "RPC succeeded but the service is not implemented on this
        # system" from "position verified": null positions are unverified, not
        # zero. Surface the per-method matrix so callers need not infer.
        positions = report.get("optics_positions")
        report["optics_verified"] = (
            [position is not None for position in positions] if positions else [False, False]
        )
        capabilities = getattr(self, "capabilities", None)
        if capabilities is not None:
            report["capabilities"] = capabilities.to_dict()
            report["capabilities"]["unimplemented_methods"] = capabilities.unimplemented()
        report["rpc_trace"] = self.rpc_trace()
        return report
