"""List services actually registered on the ZEN API Gateway via gRPC reflection.

The Gateway aggregates the API description of every connected provider
(ZEN, ZEN core, MTB, ...). Listing registered services is the only reliable
way to tell "method does not exist on this system" apart from version or
configuration problems, before attempting any controlling call.
"""

# Services the workbench requires for each XY mode. Absence is reported as a
# failed check: the corresponding workflow cannot run on this system.
REQUIRED_SERVICES = {
    "motorized": (
        "zen_api.hardware.v1.SimpleStageService",
        "zen_api.hardware.v1.StageService",
        "zen_api.lm.hardware.v2.FocusService",
        "zen_api.acquisition.v1beta.ExperimentService",
        "zen_api.acquisition.v1beta.ExperimentStreamingService",
    ),
    "no_xy": (
        "zen_api.lm.hardware.v2.FocusService",
        "zen_api.acquisition.v1beta.ExperimentService",
        "zen_api.acquisition.v1beta.ExperimentStreamingService",
    ),
}

# Needed for mark capture / AF / optics readback, but their absence does not
# block the angle-scan path; reported as a warning.
OPTIONAL_SERVICES = (
    "zen_api.lm.acquisition.v1.ExperimentSwAutofocusService",
    "zen_api.lm.acquisition.v1beta.ExperimentSwAutofocusService",
    "zen_api.lm.acquisition.v1.DefiniteFocusService",
    "zen_api.lm.hardware.v2.ObjectiveChangerService",
    "zen_api.lm.hardware.v2.OptovarService",
)


def list_services(channel, timeout=5.0):
    """Return the full names of services registered on the Gateway.

    Raises ImportError when grpcio-reflection is missing and RuntimeError
    when the reflection call itself is rejected.
    """
    from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

    stub = reflection_pb2_grpc.ServerReflectionStub(channel)
    responses = stub.ServerReflectionInfo(
        iter((reflection_pb2.ServerReflectionRequest(list_services=""),)),
        timeout=timeout,
    )
    for response in responses:
        if response.HasField("list_services_response"):
            return sorted(service.name for service in response.list_services_response.service)
        if response.HasField("error_response"):
            raise RuntimeError(
                f"reflection 查询被拒：{response.error_response.error_message}"
            )
    return ()


def file_descriptor_protos(channel, symbol, timeout=5.0):
    """Return FileDescriptorProto objects for the file containing `symbol`.

    gRPC reflection serves the exact schema the Gateway registered, which is
    the only trustworthy source when the local proto version does not match
    the installed ZEN (e.g. a v1beta service on ZEN 3.12). Raises RuntimeError
    when the Gateway refuses the request.
    """
    from google.protobuf import descriptor_pb2
    from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

    stub = reflection_pb2_grpc.ServerReflectionStub(channel)
    responses = stub.ServerReflectionInfo(
        iter((reflection_pb2.ServerReflectionRequest(file_containing_symbol=symbol),)),
        timeout=timeout,
    )
    for response in responses:
        if response.HasField("file_descriptor_response"):
            protos = []
            for raw in response.file_descriptor_response.file_descriptor_proto:
                proto = descriptor_pb2.FileDescriptorProto()
                proto.ParseFromString(raw)
                protos.append(proto)
            return protos
        if response.HasField("error_response"):
            raise RuntimeError(
                f"reflection 描述符查询被拒：{response.error_response.error_message}"
            )
    return ()


def short_name(service):
    return service.replace("zen_api.", "")
