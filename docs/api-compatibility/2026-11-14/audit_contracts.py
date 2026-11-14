"""Reproduce contract inspection from pinned official source files, without executing downloaded Python."""
from pathlib import Path
import ast,json,sys,subprocess,hashlib
from google.protobuf import descriptor_pb2
import grpc_tools
import argparse
parser=argparse.ArgumentParser(description='Compare pinned ZEISS official contracts; no hardware calls.')
parser.add_argument('--sources',type=Path,required=True,help='Cache containing official commit directories as documented in README')
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
root=args.sources;new=root/'781ce14954946366524b96347e7767cefb11a2c8'/'ZEN-API'/'protofiles'
versions={}
for base in sorted(new.iterdir()):
 out=root/(base.name+'.pb')
 subprocess.run([sys.executable,'-m','grpc_tools.protoc','-I'+str(base),'-I'+str(Path(grpc_tools.__file__).parent/'_proto'),'--include_imports','--descriptor_set_out='+str(out),*[str(x.relative_to(base)) for x in base.rglob('*.proto')]],cwd=base,check=True,capture_output=True)
 ds=descriptor_pb2.FileDescriptorSet.FromString(out.read_bytes());messages={};services={};enums={}
 def msg(pkg,m):
  messages[pkg+'.'+m.name]=m
  for child in m.nested_type:msg(pkg+'.'+m.name,child)
 for f in ds.file:
  for m in f.message_type:msg(f.package,m)
  for e in f.enum_type:enums[f.package+'.'+e.name]={v.name:v.number for v in e.value}
  for s in f.service:services[f.package+'.'+s.name]={m.name:{'request':m.input_type.lstrip('.'),'response':m.output_type.lstrip('.'),'server_streaming':m.server_streaming} for m in s.method}
 versions[base.name]={'messages':{k:{f.name:{'number':f.number,'type':descriptor_pb2.FieldDescriptorProto.Type.Name(f.type),'type_name':f.type_name.lstrip('.'),'repeated':f.label==3} for f in m.field} for k,m in messages.items()},'services':services,'enums':enums}
old=root/'c94fb25f693900c4ad23b4e4fbe3f7f3f7b57fbc'/'ZEN-API/python_examples/public'
oldmsgs={};oldservices={};oldenums={}
for p in old.rglob('*.py'):
 pkg='.'.join(p.relative_to(old).parts[:-1]);tree=ast.parse(p.read_text())
 for c in tree.body:
  if not isinstance(c,ast.ClassDef):continue
  bases=[ast.unparse(b) for b in c.bases]
  if 'betterproto.Message' in bases:
   fields={}
   for a in c.body:
    if not isinstance(a,ast.AnnAssign) or not isinstance(a.value,ast.Call):continue
    call=a.value;kind=call.func.attr.removesuffix('_field');wrap=next((ast.unparse(k.value).split('.')[-1].removeprefix('TYPE_').title()+'Value' for k in call.keywords if k.arg=='wraps'),None)
    anno=ast.unparse(a.annotation).strip("'\"")
    fields[a.target.id]={'number':ast.literal_eval(call.args[0]),'kind':kind,'annotation':anno,'wrapper':wrap,'repeated':anno.startswith(('List[','Dict['))}
   oldmsgs[pkg+'.'+c.name]=fields
  elif 'betterproto.Enum' in bases:
   oldenums[pkg+'.'+c.name]={a.targets[0].id:ast.literal_eval(a.value) for a in c.body if isinstance(a,ast.Assign)}
  elif 'betterproto.ServiceStub' in bases:
   for m in c.body:
    if not isinstance(m,ast.AsyncFunctionDef):continue
    for a in ast.walk(m):
     if isinstance(a,ast.Call) and isinstance(a.func,ast.Attribute) and a.func.attr in ('_unary_unary','_unary_stream'):
      path=ast.literal_eval(a.args[0]);service,method=path.lstrip('/').split('/')
      request=ast.literal_eval(m.args.args[1].annotation);response=ast.unparse(a.args[2])
      oldservices.setdefault(service,{})[method]={'request':pkg+'.'+request,'response':pkg+'.'+response,'server_streaming':a.func.attr=='_unary_stream'}
versions['ZEN3.12_20250509']={'messages':oldmsgs,'services':oldservices,'enums':oldenums}

inventory=[]
def add(service, methods, legacy=None, adapted=False):
    for method in methods.split():
        inventory.append((service,method,legacy,adapted))
add('zen_api.hardware.v1.SimpleStageService','GetPosition MoveTo','zen_api.lm.hardware.v2.StageService',False)
add('zen_api.hardware.v1.StageService','GetStageMotionState')
add('zen_api.lm.hardware.v2.FocusService','GetPosition MoveTo Stop')
add('zen_api.lm.hardware.v1.FocusService','GetPosition MoveTo')
add('zen_api.lm.hardware.v2.ObjectiveChangerService','GetPosition')
add('zen_api.lm.hardware.v2.OptovarService','GetPosition')
add('zen_api.acquisition.v1beta.ExperimentService','GetAvailableExperiments Load Clone Export RunSnap RunExperiment Stop GetStatus GetImageOutputPath')
add('zen_api.acquisition.v1beta.ExperimentStreamingService','MonitorExperiment')
add('zen_api.lm.acquisition.v1.ExperimentSwAutofocusService','GetAutofocusParameters SetAutofocusParameters FindAutoFocus','zen_api.lm.acquisition.v1beta.ExperimentSwAutofocusService',True)
add('zen_api.lm.acquisition.v1.ZStackService','IsZStackExperiment GetZStackInfo ModifyZStackFirstLast ModifyZStackCenterRange','zen_api.lm.acquisition.v1beta.ZStackService',True)
add('zen_api.lm.acquisition.v1.DefiniteFocusService','FindSurface')
add('zen_api.lm.hardware.v2.CameraService','GetCameras GetCamera ConfigureCamera')


def compare_messages(old_version, old_name, new_name):
    old=versions[old_version]; latest=versions['proto_2026.11.1'];seen=set();diffs=[]
    def check(a_name,b_name):
        if (a_name,b_name) in seen:return
        seen.add((a_name,b_name))
        if a_name=='google.protobuf.Empty' and b_name==a_name:return
        a=old['messages'].get(a_name);b=latest['messages'].get(b_name)
        if a is None or b is None:
            diffs.append({'unresolved':[a_name,b_name]});return
        for key in sorted(a.keys()|b.keys()):
            x,y=a.get(key),b.get(key)
            if x is None or y is None:
                diffs.append({'message':a_name,'field':key,'old':x,'latest':y});continue
            if old_version!='ZEN3.12_20250509':
                if x!=y:diffs.append({'message':a_name,'field':key,'old':x,'latest':y})
                if x['type']=='TYPE_MESSAGE' and y['type']=='TYPE_MESSAGE':check(x['type_name'],y['type_name'])
                if x['type']=='TYPE_ENUM' and old['enums'][x['type_name']]!=latest['enums'][y['type_name']]:diffs.append({'enum':x['type_name']})
                continue
            if x['number']!=y['number'] or x['kind']!=y['type'].removeprefix('TYPE_').lower() or x['repeated']!=y['repeated']:
                diffs.append({'message':a_name,'field':key,'old':x,'latest':y});continue
            if x['wrapper']:
                if y['type_name']!='google.protobuf.'+x['wrapper']:diffs.append({'wrapper':key})
            elif x['kind'] in ('message','enum'):
                short=x['annotation'].replace('List[','').rstrip(']').strip("'\"").split('.')[-1]
                if short=='timedelta':
                    if y['type_name']!='google.protobuf.Duration':diffs.append({'duration':key})
                    continue
                collection=old['messages'] if x['kind']=='message' else old['enums']
                candidates=[name for name in collection if name.endswith('.'+short)]
                match=next((name for name in candidates if name.rsplit('.',1)[0]==a_name.rsplit('.',1)[0]),None)
                if match is None and len(candidates)==1:match=candidates[0]
                if match is None:diffs.append({'unresolved_field':a_name+'.'+key});continue
                if x['kind']=='message':check(match,y['type_name'])
                else:
                    for enum_name,number in old['enums'][match].items():
                        if not any(n==number and (label==enum_name or label.endswith('_'+enum_name)) for label,n in latest['enums'][y['type_name']].items()):
                            diffs.append({'enum':match,'value':enum_name,'number':number})
    check(old_name,new_name)
    return {'compared_message_pairs':len(seen),'differences':diffs}

rows=[]
for service,method,legacy,adapted in inventory:
    baseline=versions['proto_2026.11.1']['services'][service][method]
    row={'method':service+'.'+method,'legacy_service':legacy,'legacy_routing_implemented':adapted,'versions':{}}
    for version in ('ZEN3.12_20250509','proto_2026.5.1','proto_2026.7.1','proto_2026.11.1'):
        services=versions[version]['services'];actual=service
        contract=services.get(service,{}).get(method)
        if contract is None and legacy:
            actual=legacy;contract=services.get(legacy,{}).get(method)
        result={'documented':contract is not None,'service':actual if contract else None,
                'current_route_available':contract is not None and (actual==service or adapted)}
        if contract:
            result['server_streaming']=contract['server_streaming']
            result['request']=compare_messages(version,contract['request'],baseline['request'])
            result['response']=compare_messages(version,contract['response'],baseline['response'])
        row['versions'][version]=result
    rows.append(row)
assert len(rows)==31, len(rows)
# Standard reflection uses grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo;
# list_services and file_containing_symbol are two request variants, not ZEISS RPCs.
result={'application_commit':'4867009141e76b070c8279d233f2502d65d4b8a7',
        'official_commit':'781ce14954946366524b96347e7767cefb11a2c8',
        'legacy_commit':'c94fb25f693900c4ad23b4e4fbe3f7f3f7b57fbc',
        'inspection':'Static official contracts, not microscope execution or guaranteed runtime availability',
        'zeiss_rpc_count':len(rows),'standard_reflection_request_kinds':['list_services','file_containing_symbol'],
        'methods':rows}
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print('ZEISS methods:',len(rows))
for version in ('ZEN3.12_20250509','proto_2026.5.1','proto_2026.7.1','proto_2026.11.1'):
    statuses=[r['versions'][version] for r in rows]
    diffs=[r for r in rows if any(r['versions'][version].get(k,{}).get('differences') for k in ('request','response'))]
    print(version, 'documented',sum(s['documented'] for s in statuses),'routed',sum(s['current_route_available'] for s in statuses),'field-difference rows',len(diffs))
    for row in diffs:print(' ',row['method'],row['versions'][version])
