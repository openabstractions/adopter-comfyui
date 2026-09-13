"""Build Comfy's own preparation EXE and host-folder action from pinned inputs.

This source phase produces unsigned native inputs. Existing release signing and
build_extension package their exact final bytes; this command installs nothing.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import importlib.util
import xml.etree.ElementTree as ET
import os
import shutil
import zipfile
import tempfile

HERE=Path(__file__).resolve().parent
SOURCES=('prepare_instance.c','hostfolder_action.c','instance_reader.h',
         'prepare_instance.rc','prepare_instance.manifest','build_installer.py','file_boundary.py')
SHARED=('hostfolder_io.h','user_token.h','build_extension.py','extension.wxs','build_app.py','app.wxs')
DESKTOP_SOURCE=HERE.parent/'upstream'/'desktop-source.json'
BACKEND_SOURCE=HERE.parent/'upstream'/'backend-amd-v1.json'
DESKTOP_BUILD_INPUTS=('node','pnpm','electron','vc_redist','bootstrap_python','uv','backend','sdk','go')
BACKEND_HELPER='desktop/resources/oa-backend/launch-helper.exe'
BACKEND_HELPER_CARRIER='desktop/resources/oa-backend/launch-helper-pin.json'


def desktop_helper_carrier(measured):
    return (json.dumps(dict(name='launch-helper.exe',**measured),sort_keys=True)+'\n').encode('utf-8')


def desktop_payload_files(receipt):
    """Allow only native signatures and the one exactly derived helper carrier."""
    original=receipt['desktop_files'];native=receipt['native_files'];expected={**original,**native}
    transform=receipt.get('helper_carrier')
    if BACKEND_HELPER not in native:
        if transform is not None:raise ValueError('foreign Desktop helper carrier transform')
        return expected
    unsigned=original[BACKEND_HELPER]
    def measured(raw):return dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    before=measured(desktop_helper_carrier(unsigned))
    if original.get(BACKEND_HELPER_CARRIER)!=before:
        raise ValueError('original Desktop helper carrier differs from source-built native')
    after=measured(desktop_helper_carrier(native[BACKEND_HELPER]))
    required=dict(helper_unsigned=unsigned,helper_signed=native[BACKEND_HELPER],carrier_unsigned=before,carrier_signed=after)
    if transform!=required:raise ValueError('Desktop helper carrier transform differs from verified native')
    expected[BACKEND_HELPER_CARRIER]=after
    return expected


def desktop_supplier_members(*,policy_path=None):
    """One source-selected native member retains its official supplier bytes."""
    selected=Path(policy_path) if policy_path is not None else BACKEND_SOURCE
    if selected.is_symlink() or selected.is_junction() or not selected.is_file() or selected.stat().st_size>4<<20:
        raise ValueError('backend source policy must be an ordinary bounded file')
    tool=json.loads(selected.read_bytes())['archive_tool']
    if (tool.get('name')!='7zr.exe' or tool.get('version')!='26.05'
            or tool.get('bytes')!=602624
            or tool.get('sha256')!='9bf582b894dfa665f852d7e9112b02290a1c303c2d95a21fcdc41ffcb03143b7'):
        raise ValueError('unsupported supplier-preserved Desktop native member')
    return {'desktop/resources/oa-backend/archive-tool/7zr.exe':
            {key:tool[key] for key in ('bytes','sha256')}}


def desktop_signing_members(receipt,*,policy_path=None):
    """Partition the complete native inventory without receipt-selected exemptions."""
    native=receipt.get('native_files')
    if not isinstance(native,dict) or not native:raise ValueError('Desktop native inventory is missing')
    if receipt.get('schema')==2:
        if 'supplier_preserved' in receipt:raise ValueError('legacy Desktop receipt cannot exempt native members')
        return native
    if type(receipt.get('schema')) is not int or receipt['schema']!=3:
        raise ValueError('unsupported Desktop native receipt')
    supplier=desktop_supplier_members(policy_path=policy_path)
    if receipt.get('supplier_preserved')!=supplier or any(native.get(name)!=value for name,value in supplier.items()):
        raise ValueError('Desktop supplier member differs from source-selected bytes')
    owned={name:value for name,value in native.items() if name not in supplier}
    if not owned or set(owned)&set(supplier) or set(owned)|set(supplier)!=set(native):
        raise ValueError('Desktop native signing partition is incomplete')
    return owned

_boundary_path=HERE/'file_boundary.py'
if _boundary_path.is_symlink() or _boundary_path.is_junction() or not _boundary_path.is_file():
    raise ValueError('producer boundary helper must be a plain source file')
_boundary_bytes=_boundary_path.read_bytes()
_expected_boundary=globals().pop('_admitted_boundary_pin',None)
if _expected_boundary is not None and _expected_boundary!={
        'bytes':len(_boundary_bytes),'sha256':hashlib.sha256(_boundary_bytes).hexdigest()}:
    raise ValueError('producer boundary helper differs from authenticated consumed source')
_boundary_spec=importlib.util.spec_from_file_location('comfy_file_boundary',_boundary_path)
_boundary=importlib.util.module_from_spec(_boundary_spec)
exec(compile(_boundary_bytes,str(_boundary_path),'exec'),_boundary.__dict__)


def native_host_tool_inputs(input_root,*,go_descriptor_pin):
    """Receive only the source-selected Go closure for the native adopter.

    The normal caller binds go_descriptor_pin to its selected source policy.
    Member validation is exactly the existing whole tool-tree receiving owner.
    """
    if not isinstance(go_descriptor_pin,dict) or set(go_descriptor_pin)!={'bytes','sha256'}:
        raise ValueError('native Go descriptor pin required')
    return desktop_tool_inputs(input_root,_native_go_pin=go_descriptor_pin)


def native_input_pins():
    """Selected native route; missing actual prepared inputs remain a refusal."""
    path=HERE/'desktop-host.json'
    if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size>4<<20:
        raise ValueError('native Comfy source selection unavailable')
    value=json.loads(path.read_bytes())
    if value.get('schema')!=2 or value.get('distribution')!='oa-comfyui-native':
        raise ValueError('source does not select native Comfy')
    pins=value.get('build_inputs')
    if not isinstance(pins,dict) or set(pins)!={'backend','go'}:
        raise ValueError('native Comfy requires actual prepared backend and Go descriptor pins')
    for selected in pins.values():
        if (not isinstance(selected,dict) or set(selected)!={'bytes','sha256'} or type(selected['bytes']) is not int
                or selected['bytes']<=0 or not isinstance(selected['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',selected['sha256'])):
            raise ValueError('native Comfy prepared descriptor pin refused')
    _,sources=native_host_source_inputs()
    return dict(pins,application_manifest=sources['application-manifest.json'])


def desktop_tool_inputs(input_root,*,_native_go_pin=None):
    """Read the source-selected complete tool closures, with no acquisition.

    Native receiving reuses this owner and selects only its pinned Go role.
    The legacy default retains its full original source-policy/tool admission.
    """
    if _native_go_pin is None:
        policy=custom_desktop_policy();roles=DESKTOP_BUILD_INPUTS
    else:
        policy={'build_inputs':{'go':_native_go_pin}};roles=('go',)
    root=Path(input_root).absolute()/'desktop-inputs'
    if any(parent.is_symlink() or parent.is_junction() for parent in (root,*root.parents)):
        raise ValueError('linked Desktop tool root')
    result={}
    for role in roles:
        if role=='backend':continue
        descriptor=root/(role+'.json')
        if descriptor.is_symlink() or descriptor.is_junction() or descriptor.stat().st_size>4<<20 or pin(descriptor)!=policy['build_inputs'][role]:
            raise ValueError('Desktop tool descriptor differs from admitted input: '+role)
        value=json.loads(descriptor.read_bytes());tree=root/role
        if (set(value)!={'schema','entry','files'} or type(value['schema']) is not int or value['schema']!=1 or not isinstance(value['files'],dict)
                or not value['files'] or len(value['files'])>100000 or not isinstance(value['entry'],str) or value['entry'] not in value['files']
                or role=='sdk' and set(value['files'])&{'sdk.json','sdk-pin.json'}):
            raise ValueError('invalid fixed Desktop tool closure: '+role)
        actual=set();count=0
        for name,expected in value['files'].items():
            if (not isinstance(expected,dict) or set(expected)!={'bytes','sha256'} or type(expected['bytes']) is not int
                    or expected['bytes']<0 or not isinstance(expected['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',expected['sha256'])):
                raise ValueError('invalid Desktop tool member pin')
            parts=name.split('/')
            if not name or '\\' in name or ':' in name or any(part in ('','.','..') for part in parts):
                raise ValueError('invalid Desktop tool member')
            member=tree
            for part in ('',*parts):
                if part:member=member/part
                if member.is_symlink() or member.is_junction():raise ValueError('linked Desktop tool member')
            if not member.is_file() or member.stat().st_nlink!=1 or pin(member)!=expected:
                raise ValueError('Desktop tool member differs: '+role+'/'+name)
        pending=[tree]
        while pending:
            for member in pending.pop().iterdir():
                count+=1
                if count>100000:raise ValueError('Desktop tool closure exceeds bound')
                if member.is_symlink() or member.is_junction():raise ValueError('linked Desktop tool closure')
                if member.is_dir():pending.append(member)
                elif member.is_file():actual.add(member.relative_to(tree).as_posix())
                if len(actual)+len(pending)>100000:raise ValueError('Desktop tool closure exceeds bound')
        if actual!=set(value['files']):raise ValueError('foreign Desktop tool member: '+role)
        result[role]=dict(root=tree,entry=tree/value['entry'],descriptor=descriptor,pin=pin(descriptor),files=value['files'])
    backend_launcher_inputs(result)
    return result


def stage_desktop_tools(source_root,destination):
    """Copy only the exact admitted tool members, never an ambient cache tree."""
    roles=desktop_tool_inputs(source_root)
    _stage_selected_tools(roles,destination)
    desktop_tool_inputs(destination)
    return roles


def stage_native_host_tools(source_root,destination,*,go_descriptor_pin):
    roles=native_host_tool_inputs(source_root,go_descriptor_pin=go_descriptor_pin)
    _stage_selected_tools(roles,destination)
    native_host_tool_inputs(destination,go_descriptor_pin=go_descriptor_pin)
    return roles


def _stage_selected_tools(roles,destination):
    target=Path(destination)/'desktop-inputs'
    if target.exists():raise ValueError('Desktop tool staging requires a fresh target')
    target.mkdir(parents=True)
    for role,row in roles.items():
        descriptor=row['descriptor'].read_bytes()
        if pin(row['descriptor'])!=row['pin']:raise ValueError('Desktop descriptor changed during staging')
        (target/(role+'.json')).write_bytes(descriptor)
        for name,expected in row['files'].items():
            member=row['root']/name;data=member.read_bytes()
            if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
                raise ValueError('Desktop tool changed during staging')
            out=target/role/name;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data)
    return roles


def desktop_native_inventory(directory,*,all_files=False):
    """Measure one bounded source-built tree, selecting native or all files."""
    root=Path(directory);result=[];pending=[root];seen=set();count=0
    while pending:
        folder=pending.pop()
        if folder.is_symlink() or folder.is_junction():raise ValueError('linked Desktop output folder')
        for member in folder.iterdir():
            count+=1
            if count>100000:raise ValueError('Desktop output member bound exceeded')
            if member.is_symlink() or member.is_junction():raise ValueError('linked Desktop output member')
            if member.is_dir():pending.append(member);continue
            if not member.is_file() or member.stat().st_nlink!=1:raise ValueError('aliased Desktop output member')
            name=member.relative_to(root).as_posix()
            if name.casefold() in seen:raise ValueError('case-aliased Desktop output member')
            seen.add(name.casefold())
            with member.open('rb') as stream:
                header=stream.read(64);magic=header[:2]
                if magic==b'MZ':
                    offset=int.from_bytes(header[60:64],'little') if len(header)==64 else 0
                    if not 64<=offset<=1<<20:raise ValueError('malformed Desktop PE header')
                    stream.seek(offset)
                    if stream.read(4)!=b'PE\0\0':raise ValueError('malformed Desktop PE signature')
            native=member.suffix.lower() in ('.exe','.dll','.node')
            if native!=(magic==b'MZ'):raise ValueError('Desktop native member has undeclared/malformed shape: '+name)
            if native or all_files:result.append(dict(name=name,**pin(member)))
    if not result or len(result)>(100000 if all_files else 4096):raise ValueError('Desktop inner closure is empty or over bounds')
    return sorted(result,key=lambda row:row['name'])


def _copy_tool_tree(role,destination,*,prefix=''):
    destination=Path(destination)
    if destination.exists():raise ValueError('Desktop tool destination must be fresh')
    copied=0
    for name,expected in role['files'].items():
        if prefix and not name.startswith(prefix):continue
        relative=name[len(prefix):] if prefix else name
        data=(role['root']/name).read_bytes()
        if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
            raise ValueError('Desktop staged tool changed')
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);copied+=1
    if not copied:raise ValueError('Desktop prepared dependency subtree is unavailable: '+prefix)


def _verify_tool_tree(role,destination,*,prefix=''):
    root=Path(destination);expected={name[len(prefix):]:value for name,value in role['files'].items() if name.startswith(prefix)}
    if not expected:raise ValueError('Desktop prepared dependency subtree is empty')
    found=set();pending=[root];count=0
    while pending:
        directory=pending.pop()
        if directory.is_symlink() or directory.is_junction():raise ValueError('linked Desktop prepared dependency')
        for member in directory.iterdir():
            count+=1
            if count>100000:raise ValueError('Desktop prepared dependency bound exceeded')
            if member.is_symlink() or member.is_junction():raise ValueError('linked Desktop prepared dependency')
            if member.is_dir():pending.append(member)
            elif member.is_file():
                name=member.relative_to(root).as_posix();found.add(name)
                if name not in expected or member.stat().st_nlink!=1 or pin(member)!=expected[name]:
                    raise ValueError('Desktop prepared dependency changed')
    if found!=set(expected):raise ValueError('Desktop prepared dependency closure differs')


def desktop_environment(out,tools):
    """Build tools get a task-owned home/config/cache and no signing identity."""
    out=Path(out).absolute();home=out/'tool-home';home.mkdir(parents=True,exist_ok=True)
    user=home/'npm-user.conf';glob=home/'npm-global.conf'
    user.write_bytes(b'');glob.write_bytes(b'')
    original={key.upper():value for key,value in os.environ.items()}
    environment=_boundary.windows_build_environment(home)
    for key in ('CANDIDATE_SHA','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'):
        if key in original:environment[key]=original[key]
    environment.update(HOME=str(home),USERPROFILE=str(home),APPDATA=str(home/'roaming'),
        LOCALAPPDATA=str(home/'local'),TEMP=str(home),TMP=str(home),
        PATH=os.pathsep.join((str(tools['node']['entry'].parent),str(tools['pnpm']['entry'].parent),
                             environment['PATH'])),
        npm_config_userconfig=str(user),npm_config_globalconfig=str(glob),npm_config_cache=str(home/'npm-cache'),
        npm_config_offline='true',npm_config_ignore_scripts='true',
        ELECTRON_SKIP_BINARY_DOWNLOAD='1',CSC_IDENTITY_AUTO_DISCOVERY='false')
    return environment


def backend_launcher_inputs(tools):
    """Read the required launcher role before Desktop staging or build effects."""
    if 'go' not in tools:raise ValueError('Go role requires its exact SDK, provider-kit module and prepared offline proxy')
    role=tools['go'];root=role['root'];files=role['files']
    required={'archive.zip','sdk-receipt.json','module/go.mod','module/go.sum','go/bin/go.exe'}
    if (not required<=set(files) or role['entry']!=root/'go/bin/go.exe'
            or not any(name.startswith('proxy/') for name in files)
            or any(not (name in {'archive.zip','sdk-receipt.json'} or name.startswith(('go/','module/','proxy/'))) for name in files)):
        raise ValueError('Go role requires its exact SDK, provider-kit module and prepared offline proxy')
    module=root/'module';mod=(module/'go.mod').read_text(encoding='utf-8')
    if (not re.search(r'(?m)^module github\.com/openabstractions/abstraction-provider-kit/go\s*$',mod)
            or re.search(r'(?m)^\s*replace\b',mod)):
        raise ValueError('Go helper requires the prepared provider-kit module without external replacements')
    source=HERE/'backendlaunch/main_windows.go'
    if source.is_symlink() or source.is_junction() or not source.is_file():
        raise ValueError('source-owned backend launcher is unavailable')
    source_pin=pin(source)
    return module,source,source_pin


NATIVE_HOST_FILES=('main_windows.go','native_host_windows.go','native_resources_windows.go',
                   'native_resources.rc','application-manifest.json')


def native_host_source_inputs():
    """Exact selected native source; the archived Desktop path stays separate."""
    root=HERE/'backendlaunch'
    paths={name:root/name for name in NATIVE_HOST_FILES}
    if any(path.is_symlink() or path.is_junction() or not path.is_file() for path in paths.values()):
        raise ValueError('native Comfy host source is unavailable')
    manifest=json.loads(paths['application-manifest.json'].read_bytes())
    if (manifest.get('version')!='applications@3' or manifest.get('name')!='comfyui'
            or manifest.get('host',{}).get('arguments')!=['owned-host']):
        raise ValueError('native Comfy own-image manifest differs')
    return paths,{name:pin(path) for name,path in paths.items()}


def native_graph_source_inputs():
    """The selected wrapper source required by this native host's open path."""
    private=HERE==HERE.parents[2]/'adopters/comfyui/installer' and (HERE.parents[2]/'CLAUDE.md').is_file()
    node=HERE.parent/'node' if private else HERE/'node'
    names=('oa_appmodel_bridge.py','web/oa_appmodel.js','web/oa_appmodel_graph.mjs')
    paths={name:node/name for name in names}
    if any(not path.is_file() or path.is_symlink() or path.is_junction() or path.stat().st_nlink!=1 or path.stat().st_size>4<<20 for path in paths.values()):
        raise ValueError('native Comfy graph source closure is unavailable')
    return paths,{name:pin(path) for name,path in paths.items()}


def require_native_graph_manifest(backend,paths,sources):
    """Join source roles to the held backend manifest without expanding it.

    This receiving owner checks the graph bridge the backend carries is these
    sources before compiling a host that can request native open. The page
    uses ComfyUI's extension surface, so no frontend module is bound.
    """
    rows=backend.get('runtime_files',{})
    prefix='ComfyUI/custom_nodes/abstraction_downloads/'
    if not isinstance(rows,dict) or any(rows.get(prefix+name)!=value for name,value in sources.items()):
        raise ValueError('native Comfy backend lacks its current bootstrap graph integration')


def build_native_host(tools,shared,out,destination,*,backend_manifest,control_manifest,rc,cvtres,
                      private_workspace=None,private_sdk_receipt=None,private_source_commit=None):
    """Normal unsigned native phase; callers sign/package its exact output.

    All tools and both manifests are explicit already-admitted inputs. No
    hosted receipt, signature, registration or installation is synthesized.
    """
    paths,sources=native_host_source_inputs()
    graph_paths,graph_sources=native_graph_source_inputs()
    inputs={}
    for name,path,limit in (('backend',Path(backend_manifest),16<<20),('control',Path(control_manifest),16<<20)):
        if path.is_symlink() or path.is_junction() or not path.is_file() or not 0<path.stat().st_size<=limit:
            raise ValueError('native '+name+' manifest is unavailable')
        inputs[name]=dict(path=path,pin=pin(path))
    require_native_graph_manifest(json.loads(inputs['backend']['path'].read_bytes()),graph_paths,graph_sources)
    compilers={name:Path(path) for name,path in (('rc',rc),('cvtres',cvtres))}
    if any(not path.is_absolute() or path.is_symlink() or path.is_junction() or not path.is_file() for path in compilers.values()):
        raise ValueError('native resource tools require exact ordinary inputs')
    tool_pins={name:pin(path) for name,path in compilers.items()}
    context=dict(paths=paths,sources=sources,inputs=inputs,compilers=compilers,tool_pins=tool_pins)
    if private_workspace is None and private_sdk_receipt is None and private_source_commit is None:
        result=build_backend_launcher(tools,shared,out,destination,native_context=context)
    elif private_workspace is not None and private_sdk_receipt is not None and private_source_commit is not None and not tools:
        result=_build_local_native_host(shared,destination,context,Path(private_workspace),Path(private_sdk_receipt),private_source_commit)
    else:
        raise ValueError('private native build requires only its explicit workspace and SDK receipt')
    if (sources!={name:pin(path) for name,path in paths.items()} or graph_sources!={name:pin(path) for name,path in graph_paths.items()}
            or any(pin(item['path'])!=item['pin'] for item in inputs.values())):
        raise ValueError('native Comfy inputs changed during build')
    if tool_pins!={name:pin(path) for name,path in compilers.items()}:
        raise ValueError('native Comfy resource tools changed during build')
    return dict(result,source_files=sources,graph_sources=graph_sources,resources={name:item['pin'] for name,item in inputs.items()},
                resource_tools=tool_pins,application_manifest=sources['application-manifest.json'])


def _stage_native_sources(stage,context):
    for name,path in context['paths'].items():shutil.copyfile(path,stage/name)
    for name,item in context['inputs'].items():
        (stage/(name+'-pin.json')).write_bytes((json.dumps(item['pin'],sort_keys=True)+'\n').encode('utf-8'))
    shutil.copyfile(context['inputs']['backend']['path'],stage/'backend-manifest.json')


def _compile_native_resources(shared,stage,target,receipt,environment,context):
    subprocess.run([receipt['go'],'list','-deps','.'],
        cwd=stage,env=environment,check=True,timeout=60,stdout=subprocess.DEVNULL)
    resource=stage/'native.res';coff=stage/'native_windows_amd64.syso'
    subprocess.run([str(context['compilers']['rc']),'/nologo','/fo',str(resource),str(stage/'native_resources.rc')],
        cwd=stage,env=environment,check=True,timeout=60)
    subprocess.run([str(context['compilers']['cvtres']),'/NOLOGO','/MACHINE:X64','/OUT:'+str(coff),str(resource)],
        cwd=stage,env=environment,check=True,timeout=60)
    resource_sources=backend_resource_tool_sources(shared)
    resource_pins={str(path):pin(path) for path in resource_sources}
    subprocess.run([receipt['go'],'run','-trimpath',*map(str,resource_sources),'-sanitize-coff',str(coff)],
        cwd=stage,env=environment,check=True,timeout=60)
    if resource_pins!={str(path):pin(path) for path in resource_sources}:
        raise ValueError('Go resource sanitizer source changed during build')
    subprocess.run([receipt['go'],'build','-trimpath','-o',str(target),'.'],
        cwd=stage,env=environment,check=True,timeout=300)
    return {path.name:resource_pins[str(path)] for path in resource_sources}


def private_native_source_identity(workspace,source_commit,sdk_receipt,compilers):
    """Receive the same clean private revision and exact compiler input closure."""
    workspace=Path(workspace).absolute()
    if (not isinstance(source_commit,str) or not re.fullmatch('[0-9a-f]{40}',source_commit)
            or not (workspace/'CLAUDE.md').is_file() or not (workspace/'go.work').is_file()):
        raise ValueError('private native source revision required')
    source_path=workspace/'scripts/package_comfyui_app.py'
    original=list(sys.path)
    try:
        sys.path.insert(0,str(workspace/'scripts'))
        spec=importlib.util.spec_from_file_location('comfy_private_source_revision_owner',source_path)
        source_owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(source_owner)
        source_owner._require_private_source_revision(workspace,source_commit)
    finally:sys.path[:]=original
    sdk_path=backend_build_owner_paths(workspace/'installer/apps')['sdk']
    spec=importlib.util.spec_from_file_location('comfy_local_windows_go_sdk',sdk_path)
    sdk=importlib.util.module_from_spec(spec);spec.loader.exec_module(sdk)
    sdk_receipt=Path(sdk_receipt).absolute()
    receipt=json.loads(sdk_receipt.read_bytes())
    sdk.validate_sdk_receipt(receipt)
    if sdk_receipt!=Path(receipt['receipt_path']):raise ValueError('private native SDK receipt path differs')
    if set(compilers)!={'rc','cvtres'}:raise ValueError('private native resource compiler roles required')
    tools={}
    for name,path in compilers.items():
        path=Path(path)
        if not path.is_absolute():
            raise ValueError('private native resource compiler must be ordinary')
        _boundary.ordinary_path(path,kind='file')
        tools[name]=pin(path)
    _,sources=native_host_source_inputs()
    _,graph_sources=native_graph_source_inputs()
    identity=dict(source_commit=source_commit,source_files=sources,graph_sources=graph_sources,
        compiler=receipt['source_identity'],sdk_receipt_path=str(sdk_receipt),sdk_receipt=pin(sdk_receipt),
        resource_tool_paths={name:str(Path(path)) for name,path in compilers.items()},resource_tools=tools,
        resource_postprocess={path.name:pin(path) for path in backend_resource_tool_sources(workspace/'installer/apps')})
    return identity,sdk


def _build_local_native_host(shared,destination,context,workspace,sdk_receipt,source_commit):
    """Explicit source-checkout preview compile; hosted input admission stays separate."""
    workspace=workspace.absolute()
    if (workspace!=shared.parents[1] or not (workspace/'CLAUDE.md').is_file()
            or not (workspace/'go.work').is_file() or workspace.is_symlink() or workspace.is_junction()):
        raise ValueError('private native build requires this exact source workspace')
    destination=Path(destination)
    _boundary.ordinary_path(destination,kind='directory')
    if (any((destination/name).exists() or (destination/name).is_symlink()
                   for name in ('launch-helper.exe','launch-helper-pin.json'))):
        raise ValueError('private native output must be a fresh ordinary directory')
    if not os.environ.get('GOCACHE') or not os.environ.get('GOMODCACHE'):
        raise ValueError('private native build requires explicitly admitted Go caches')
    identity,sdk=private_native_source_identity(workspace,source_commit,sdk_receipt,context['compilers'])
    receipt=json.loads(sdk_receipt.read_bytes())
    module=workspace/'openabstractions-flat/abstraction-provider-kit/go'
    _boundary.ordinary_path(module,kind='directory')
    if not (module/'go.mod').is_file():
        raise ValueError('private native provider-kit module unavailable')
    parent=module/'.forks'
    parent.mkdir(exist_ok=True)
    if parent.is_symlink() or parent.is_junction():raise ValueError('private native stage parent is linked')
    stage=Path(tempfile.mkdtemp(prefix='comfy-local-',dir=parent))
    try:
        _stage_native_sources(stage,context)
        environment=sdk.sdk_environment(receipt,_boundary.windows_build_environment(stage))
        environment.update(CGO_ENABLED='0',GOOS='windows',GOARCH='amd64',GOWORK=str(workspace/'go.work'),
            GOENV='off',GOTOOLCHAIN='local',GOFLAGS='-mod=readonly',GOPROXY='off',GOSUMDB='off',GOTELEMETRY='off')
        if os.environ.get('GOCACHE'):environment['GOCACHE']=os.environ['GOCACHE']
        if os.environ.get('GOMODCACHE'):environment['GOMODCACHE']=os.environ['GOMODCACHE']
        target=stage/'launch-helper.exe'
        resource_pins=_compile_native_resources(shared,stage,target,receipt,environment,context)
        after,_=private_native_source_identity(workspace,source_commit,sdk_receipt,context['compilers'])
        if identity!=after:raise ValueError('private native source/compiler input closure changed')
        measured=pin(target)
        shutil.copyfile(target,destination/'launch-helper.exe')
        if pin(destination/'launch-helper.exe')!=measured:raise ValueError('private native output copy differs')
        (destination/'launch-helper-pin.json').write_bytes(desktop_helper_carrier(measured))
        result=dict(source_mode='private-local-preview',source=pin(context['paths']['main_windows.go']),
            compiler=receipt['source_identity'],native=measured,resource_postprocess=resource_pins,
            private_source_identity=identity)
    except BaseException as error:
        error.add_note('Private Comfy native build retained at '+str(stage))
        raise
    _boundary.remove_tree(stage)
    return result


def backend_build_owner_paths(shared):
    """Select the existing private source or public redist build owners."""
    redist=shared.parents[1]
    private=shared==redist/'installer/apps' and (redist/'CLAUDE.md').is_file() and (redist/'scripts/split.manifest').is_file()
    return {'sdk':redist/('scripts/windows_go_sdk.py' if private else 'tools/windows_go_sdk.py'),
            'stage':redist/'scripts/stage_platform.py'}


def backend_resource_tool_sources(shared):
    redist=shared.parents[1]
    private=backend_build_owner_paths(shared)['sdk']==redist/'scripts/windows_go_sdk.py'
    root=redist/('monitor/icon/gen' if private else 'tools/windows-resource-gen')
    # The public projection carries the same complete standard-library command.
    # Keep one roster for compilation, pre/post pins and source identity.
    paths=[root/name for name in ('main.go','native.go','agent_release.go')]
    if any(not path.is_file() or path.is_symlink() or path.is_junction() for path in paths):
        raise ValueError('selected Go resource sanitizer source unavailable')
    return paths


def build_backend_launcher(tools,shared,out,destination,*,native_context=None):
    """Compile one explicit app-owned main using the admitted offline Go closure."""
    module,source,source_pin=backend_launcher_inputs(tools)
    for name in ('launch-helper.exe','launch-helper-pin.json'):
        target=_boundary.extended_path(destination/name)
        if (any(path.is_symlink() or path.is_junction() for path in (target,*target.parents))
                or target.exists() and (not target.is_file() or target.stat().st_nlink!=1)):
            raise ValueError('backend launcher output must be ordinary owned files')
    redist=shared.parents[1];search=[str(redist/'scripts'),str(redist/'tools')]
    original=list(sys.path)
    try:
        sys.path[:0]=search
        # Import only the selected existing build owners, never an ambient SDK.
        owners={}
        for name,path in backend_build_owner_paths(shared).items():
            if path.is_symlink() or path.is_junction() or not path.is_file():
                raise ValueError('selected Windows Go build owner unavailable')
            spec=importlib.util.spec_from_file_location('desktop_go_'+name,path)
            owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner);owners[name]=owner
    finally:sys.path[:]=original
    # SDK testdata and Go tools require ordinary child-process paths. Keep the
    # complete private build independent of a caller's potentially deep output.
    work=Path(tempfile.mkdtemp(prefix='comfy-go-',dir=Path(tempfile.gettempdir())))
    try:
        result=_compile_backend_launcher(tools,shared,destination,module,source,source_pin,owners,work,native_context)
    except BaseException as error:
        # A completed import-admission refusal launched no compiler. Other
        # failures retain the exact work path for the native lifetime owner:
        # subprocess timeout alone does not prove descendant closure.
        if isinstance(error,subprocess.CalledProcessError) and error.cmd[1:3]==['list','-deps']:
            _boundary.remove_tree(work)
        else:
            error.add_note('Comfy native build recovery work retained at '+str(work))
        raise
    _boundary.remove_tree(work)
    return result


def _compile_backend_launcher(tools,shared,destination,module,source,source_pin,owners,work,native_context):
    role=tools['go'];root=role['root'];files=role['files'];sdk=owners['sdk']
    sdk._private_root(work,create=True)
    receipt=sdk.prepare_sdk(work/'sdk',archive_path=root/'archive.zip')
    original_receipt=json.loads((root/'sdk-receipt.json').read_bytes())
    if (receipt['source_identity']!=original_receipt.get('source_identity')
            or pin(Path(receipt['go']))!=files['go/bin/go.exe']):
        raise ValueError('prepared Go SDK differs from source-selected compiler identity')
    environment=_boundary.windows_build_environment(work)
    environment,receipt=owners['stage'].windows_sdk_environment(work/'sdk',
        dict(environment,OA_WINDOWS_GO_SDK_RECEIPT=receipt['receipt_path']),sdk_owner=sdk)
    for name in ('gocache','gomod','gotmp','home'):
        sdk._private_root(work/name,create=True)
    environment.update(CGO_ENABLED='0',GOOS='windows',GOARCH='amd64',GOWORK='off',GOENV='off',
        GOTOOLCHAIN='local',GOFLAGS='-mod=readonly',GOPROXY=(root/'proxy').as_uri(),GOSUMDB='off',
        GONOSUMDB='*',GOCACHE=str(work/'gocache'),GOMODCACHE=str(work/'gomod'),GOTMPDIR=str(work/'gotmp'),
        HOME=str(work/'home'),USERPROFILE=str(work/'home'),GOTELEMETRY='off')
    target=work/'launch-helper.exe'
    if native_context is None:
        subprocess.run([receipt['go'],'build','-trimpath','-o',str(target),str(source)],
            cwd=module,env=environment,check=True,timeout=300)
    else:
        stage=work/'source'
        # The host imports provider-kit packages (including enginelaunch)
        # under this same module identity. Preserve its admitted source tree.
        _copy_tool_tree(role,stage,prefix='module/')
        _verify_tool_tree(role,stage,prefix='module/')
        _stage_native_sources(stage,native_context)
        # Admit the effective host's full import closure with the same immutable
        # module and offline environment before invoking any compiler.
        resource_postprocess=_compile_native_resources(shared,stage,target,receipt,environment,native_context)
    if pin(source)!=source_pin:raise ValueError('backend launcher source changed during compilation')
    sdk.validate_sdk_receipt(receipt)
    if not target.is_file() or target.is_symlink() or target.is_junction():
        raise ValueError('backend launcher compiler produced no ordinary executable')
    measured=pin(target)
    final=_boundary.extended_path(destination/'launch-helper.exe')
    shutil.copyfile(target,final)
    if final.is_symlink() or final.is_junction() or final.stat().st_nlink!=1 or pin(final)!=measured:
        raise ValueError('backend launcher output changed during copy')
    _boundary.extended_path(destination/'launch-helper-pin.json').write_bytes(desktop_helper_carrier(measured))
    result=dict(source=source_pin,compiler=receipt['source_identity'],native=measured)
    if native_context is not None:
        result['resource_postprocess']=resource_postprocess
    return result


def desktop_prepare(args,shared,out):
    """Build the upstream Desktop directory from complete admitted local inputs.

    Dependency acquisition/native dependency preparation is a separate input
    admission. No install/postinstall/reify or supplier fallback runs here.
    """
    policy=custom_desktop_policy();tools=desktop_tool_inputs(args.input_root)
    if any(path.is_symlink() or path.is_junction() for path in (out,*out.parents)):
        raise ValueError('linked Desktop build output')
    if args.source_root is None or args.input_receipt is None:
        raise ValueError('Desktop build requires exact upstream source and measured producer inputs')
    if out.exists():raise ValueError('Desktop directory build requires fresh output')
    source=args.source_root.absolute();received_pin=pin(args.input_receipt)
    received=json.loads(args.input_receipt.read_bytes())
    def input_file(name):
        target=Path(args.input_root)/name
        expected=received.get('build_inputs',{}).get(name)
        if (not isinstance(expected,dict) or target.is_symlink() or target.is_junction()
                or not target.is_file() or pin(target)!=expected):raise ValueError('unmeasured Desktop build input: '+name)
        return target
    icon=input_file('oa-comfyui.ico')
    resource_path=input_file('backend/desktop-backend.json')
    resource_raw=resource_path.read_bytes()
    pair_path=shared.parents[1]/'scripts/app_pair_receipt.py'
    pair_spec=importlib.util.spec_from_file_location('desktop_resource_owner',pair_path)
    pair=importlib.util.module_from_spec(pair_spec)
    exec(compile(pair_path.read_bytes(),str(pair_path),'exec'),pair.__dict__)
    resource=pair.validate_backend_resource(json.loads(resource_raw))
    if resource['provenance']['input_descriptor']!={'name':'backend-inputs.json',**policy['build_inputs']['backend']}:
        raise ValueError('Desktop resource backend input differs from selected policy')
    backend_policy=input_file('backend/backend-amd-v1.json')
    if backend_policy.read_bytes()!=BACKEND_SOURCE.read_bytes():
        raise ValueError('Desktop backend recipe differs from selected source policy')
    if not any(name.startswith('node_modules/') for name in tools['pnpm']['files']):
        raise ValueError('Desktop pnpm input lacks its complete prepared Electron-compatible dependency closure')
    if (shared.parent/'certum_simplysign.json').is_symlink() or pin(shared.parent/'certum_simplysign.json')!=policy['signing_policy']:
        raise ValueError('Desktop source signing policy changed')
    out.mkdir(parents=True)
    prepare_desktop_source(source,out/'source-preparation',args.git,git_environment_owner=shared.parents[1]/'scripts/git_env.py')
    _copy_tool_tree(tools['pnpm'],source/'node_modules',prefix='node_modules/')
    _verify_tool_tree(tools['pnpm'],source/'node_modules',prefix='node_modules/')
    _copy_tool_tree(tools['bootstrap_python'],source/'bootstrap-python/win-x64')
    uv=tools['uv']['entry'];uv_target=source/'bootstrap-python/win-x64/uv.exe'
    if uv_target.exists() and pin(uv_target)!=pin(uv):raise ValueError('Desktop bootstrap UV inputs disagree')
    uv_target.write_bytes(uv.read_bytes())
    resources=source/'resources';resources.mkdir(exist_ok=True)
    (resources/'oa-comfyui.ico').write_bytes(icon.read_bytes())
    (resources/'vc_redist.x64.exe').write_bytes(tools['vc_redist']['entry'].read_bytes())
    packaged_backend=source/'oa-backend';packaged_backend.mkdir()
    for row in resource['provenance'].values():
        member=input_file('backend/'+row['name'])
        if pin(member)!={key:row[key] for key in ('bytes','sha256')}:
            raise ValueError('Desktop resource provenance changed')
        shutil.copyfile(member,packaged_backend/row['name'])
    manifest=json.loads((packaged_backend/'backend-manifest.json').read_bytes())
    if (manifest.get('id')!='win-amd' or manifest.get('manager') is not False
            or manifest.get('input_descriptor_sha256')!=policy['build_inputs']['backend']['sha256']
            or manifest.get('comfyui_ref')!=manifest.get('comfyui_commit')
            or not re.fullmatch('[0-9a-f]{40}',str(manifest.get('comfyui_ref')))
            or not re.fullmatch(r'3\.12\.[0-9]+',str(manifest.get('python_version')))):
        raise ValueError('Desktop backend manifest differs from admitted AMD input')
    if (manifest['comfyui_ref']!=resource['comfyui_ref'] or manifest['python_version']!=resource['python_version']):
        raise ValueError('Desktop resource manifest identity changed')
    (packaged_backend/'desktop-backend.json').write_bytes(resource_raw)
    tool_policy=json.loads(backend_policy.read_bytes())['archive_tool']
    for row,name in [(tool_policy,tool_policy['name']),(tool_policy['source'],tool_policy['source']['name']),
                     *[(item,'notices/'+item['name']) for item in tool_policy['notices']]]:
        member=input_file('archive-tool/'+name)
        if pin(member)!={key:row[key] for key in ('bytes','sha256')}:
            raise ValueError('Desktop extractor input differs from selected source')
        target=packaged_backend/'archive-tool'/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(member,target)
    bridge=input_file('desktop_backend.cjs');shutil.copyfile(bridge,packaged_backend/bridge.name)
    build_backend_launcher(tools,shared,out,packaged_backend)
    sdk=source/'oa-sdk';_copy_tool_tree(tools['sdk'],sdk)
    (sdk/'sdk.json').write_bytes(tools['sdk']['descriptor'].read_bytes())
    (sdk/'sdk-pin.json').write_text(json.dumps(dict(name='sdk.json',**policy['build_inputs']['sdk']))+'\n',encoding='utf-8')
    config=out/'desktop-build.json'
    config.write_text(json.dumps(dict(extends=str(source/'electron-builder.yml'),
        directories=dict(output=str(out/'desktop-output')),electronDist=str(tools['electron']['entry'].parent),
        electronVersion='40.4.1',npmRebuild=False,nodeGypRebuild=False,publish=None,
        win=dict(signtoolOptions=dict(signingHashAlgorithms=['sha256'],publisherName=None)))),encoding='utf-8')
    environment=desktop_environment(out,tools)
    pnpm=([str(tools['node']['entry']),str(tools['pnpm']['entry'])] if tools['pnpm']['entry'].suffix=='.cjs' else [str(tools['pnpm']['entry'])])
    subprocess.run([*pnpm,'run','build'],cwd=source,env=environment,check=True)
    subprocess.run([*pnpm,'exec','electron-builder','--dir','--win','--x64','--publish','never','--config',str(config)],cwd=source,env=environment,check=True)
    built=out/'desktop-output/win-unpacked'
    if not built.is_dir() or built.is_symlink() or built.is_junction():raise ValueError('Desktop directory output is missing or redirected')
    built.rename(out/'desktop')
    native={('desktop/'+row['name']):{key:row[key] for key in ('sha256','bytes')} for row in desktop_native_inventory(out/'desktop')}
    name='desktop/'+policy['identity']['exe_name']
    if name not in native:raise ValueError('Desktop output lacks its source-owned native executable')
    certificate=json.loads((shared.parent/'certum_simplysign.json').read_bytes())['certificate']
    receipt=dict(schema=3,stage='desktop-inner-unsigned',version=args.version,
        source_policy=policy['source_policy'],build_inputs=policy['build_inputs'],identity=policy['identity'],
        host=dict(arp_id=policy['identity']['arp_id'],version=desktop_source_policy()[0]['upstream']['version'],
            exe=dict(name=name,**native[name]),publisher=dict(subject=certificate['subject'],thumbprint=certificate['sha1'])),
        native_files=native,supplier_preserved=desktop_supplier_members(),desktop_files={'desktop/'+row['name']:{key:row[key] for key in ('sha256','bytes')}
            for row in desktop_native_inventory(out/'desktop',all_files=True)},
        input_receipt=received_pin,source_preparation=pin(out/'source-preparation/desktop-source-receipt.json'))
    if pin(args.input_receipt)!=received_pin:raise ValueError('Desktop producer inputs changed during build')
    desktop_signing_members(receipt)
    input_file('backend/desktop-backend.json');input_file('oa-comfyui.ico')
    _verify_tool_tree(tools['pnpm'],source/'node_modules',prefix='node_modules/')
    desktop_tool_inputs(args.input_root)
    (out/'desktop-native-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    return 0

def custom_desktop_policy(*,require_admitted=True):
    """Immutable source policy is independent of variable compiled artifact bytes."""
    policy=json.loads((HERE/'desktop-host.json').read_bytes())
    if set(policy)!={'schema','distribution','identity','source_policy','signing_policy','build_inputs'} or \
       policy['schema']!=2 or policy['distribution']!='oa-custom-desktop':
        raise ValueError('custom Desktop requires source-owned schema2 policy')
    source,_=desktop_source_policy()
    if policy['source_policy']!=pin(DESKTOP_SOURCE) or policy['identity']!=source['identity']:
        raise ValueError('custom Desktop identity/source policy differs')
    missing=set(DESKTOP_BUILD_INPUTS)-set(policy['build_inputs'])
    if require_admitted and missing:
        raise ValueError('custom Desktop build inputs require admission: '+', '.join(sorted(missing)))
    if set(policy['build_inputs'])-set(DESKTOP_BUILD_INPUTS):raise ValueError('unknown custom Desktop build input')
    for row in policy['build_inputs'].values():
        if not isinstance(row,dict) or set(row)!={'sha256','bytes'} or not re.fullmatch('[0-9a-f]{64}',row['sha256']) or type(row['bytes']) is not int or row['bytes']<=0:
            raise ValueError('custom Desktop build input lacks exact admitted bytes')
    return policy

def _desktop_member(root,row):
    if (not isinstance(row,dict) or set(row)!={'name','sha256','bytes'} or not isinstance(row['name'],str)
            or '\\' in row['name'] or ':' in row['name'] or any(part in ('','.','..') for part in row['name'].split('/'))
            or not re.fullmatch('[0-9a-f]{64}',str(row['sha256'])) or type(row['bytes']) is not int or row['bytes']<=0):
        raise ValueError('invalid custom Desktop member')
    target=Path(root).absolute()
    for part in (*reversed(target.parents),target):
        if part.is_symlink() or part.is_junction():raise ValueError('linked custom Desktop root')
    for part in row['name'].split('/'):
        target=target/part
        if target.is_symlink() or target.is_junction():raise ValueError('linked custom Desktop member')
    if not target.is_file() or target.stat().st_nlink!=1 or pin(target)!={key:row[key] for key in ('bytes','sha256')}:
        raise ValueError('custom Desktop member differs: '+row['name'])
    return target


def _desktop_receipt(root,name,expected,stage,signing_policy,*,source,run,attempt):
    policy=custom_desktop_policy()
    if pin(signing_policy)!=policy['signing_policy']:raise ValueError('Desktop signing policy changed')
    path=_desktop_member(root,dict(name=name,**expected))
    if path.stat().st_size>4<<20:raise ValueError('Desktop receipt exceeds bound')
    value=json.loads(path.read_bytes())
    if (type(value.get('schema')) is not int or value['schema'] not in (2,3) or value.get('stage')!=stage or value.get('source_policy')!=policy['source_policy']
            or value.get('build_inputs')!=policy['build_inputs'] or value.get('identity')!=policy['identity']
            or value.get('source')!=source or value.get('run_id')!=run or value.get('run_attempt')!=attempt
            or not re.fullmatch('[0-9a-f]{40}',str(source)) or type(run) is not int or run<=0
            or type(attempt) is not int or attempt<=0):
        raise ValueError('Desktop receipt differs from authenticated stage/source/run/attempt')
    certificate=json.loads(Path(signing_policy).read_bytes())['certificate']
    host=value.get('host',{})
    if (set(host)!={'arp_id','version','exe','publisher'} or host['arp_id']!=policy['identity']['arp_id']
            or host['version']!=desktop_source_policy()[0]['upstream']['version']
            or host['publisher']!={'subject':certificate['subject'],'thumbprint':certificate['sha1']}
            or host['exe'].get('name')!='desktop/'+policy['identity']['exe_name']):
        raise ValueError('Desktop receipt host identity differs')
    native=value.get('native_files')
    if not isinstance(native,dict) or not native or len(native)>4096:raise ValueError('Desktop native closure missing')
    for member,measured in native.items():_desktop_member(root,dict(name=member,**measured))
    actual={'desktop/'+row['name']:{key:row[key] for key in ('bytes','sha256')} for row in desktop_native_inventory(Path(root)/'desktop')}
    if actual!=native or host['exe']!=dict(name='desktop/'+policy['identity']['exe_name'],**native.get('desktop/'+policy['identity']['exe_name'],{})):
        raise ValueError('Desktop native closure differs from receipt')
    desktop_signing_members(value)
    original=value.get('desktop_files')
    if not isinstance(original,dict) or not original or len(original)>100000 or not set(native)<=set(original):
        raise ValueError('Desktop complete original file closure missing')
    for name,measured in original.items():
        if (not isinstance(name,str) or not name.startswith('desktop/') or '\\' in name or ':' in name
                or any(part in ('','.','..') for part in name.split('/')) or not isinstance(measured,dict)
                or set(measured)!={'bytes','sha256'} or type(measured['bytes']) is not int or measured['bytes']<0
                or not re.fullmatch('[0-9a-f]{64}',str(measured['sha256']))):
            raise ValueError('Desktop original file role differs')
    expected=desktop_payload_files(value)
    complete={'desktop/'+row['name']:{key:row[key] for key in ('bytes','sha256')}
        for row in desktop_native_inventory(Path(root)/'desktop',all_files=True)}
    if complete!=expected:raise ValueError('Desktop complete file closure differs from original build')
    if value.get('signatures',{}).get('name')!='desktop-signatures.json' or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',str(value.get('version'))):
        raise ValueError('Desktop receipt signature/version role differs')
    _desktop_member(root,value['signatures'])
    return value


def signed_desktop(args,shared,out):
    """Receive an API-authenticated signed-inner record; caller owns crypto verification."""
    if args.signed_desktop_receipt is None or args.signed_desktop_receipt.absolute()!=out/'desktop-signed-receipt.json':
        raise ValueError('signed Desktop receipt must be in the owned native output root')
    value=_desktop_receipt(out,'desktop-signed-receipt.json',
        dict(bytes=args.signed_desktop_bytes,sha256=args.signed_desktop_sha256),'desktop-inner-signed',
        shared.parent/'certum_simplysign.json',source=args.producer_source,run=args.producer_run,attempt=args.producer_attempt)
    allowed={'schema','stage','version','source_policy','build_inputs','identity','source','run_id','run_attempt',
             'host','native_files','desktop_files','signatures','input_receipt','source_preparation'}
    if value['schema']==3:allowed.add('supplier_preserved')
    if BACKEND_HELPER in value.get('native_files',{}):allowed.add('helper_carrier')
    if set(value)!=allowed:raise ValueError('signed Desktop receipt fields differ')
    if value['version']!=args.version:raise ValueError('signed Desktop release version differs')
    return value


def custom_desktop_package(root,authenticated_receipt_pin,*,expected_source,expected_run,expected_attempt,
                           signing_policy,installer_root=None,signed_installer=None):
    """Read the immutable unsigned package intent and its exact signed native inputs.

    The caller authenticates this receipt and raw installer through API intake.
    An optional fresh signature observation describes the later transformation;
    the original unsigned installer pin remains in the package record.
    """
    value=_desktop_receipt(root,'package/desktop-package-receipt.json',authenticated_receipt_pin,
        'desktop-packaged-unsigned',signing_policy,source=expected_source,run=expected_run,attempt=expected_attempt)
    allowed={'schema','stage','version','source_policy','build_inputs','identity','source','run_id','run_attempt',
                     'host','native_files','desktop_files','signatures','installer','helper_receipt','helpers'}
    if value['schema']==3:allowed.add('supplier_preserved')
    if BACKEND_HELPER in value.get('native_files',{}):allowed.add('helper_carrier')
    if set(value)!=allowed:
        raise ValueError('Desktop package fields differ')
    installer=value['installer'];name=installer.get('name','')
    if not re.fullmatch(r'OpenAbstractions-ComfyUI-[0-9]+\.[0-9]+\.[0-9]+-win-x64\.exe',name):
        raise ValueError('Desktop installer role differs')
    target_root=Path(installer_root) if installer_root is not None else Path(root)/'package'
    if signed_installer is None:_desktop_member(target_root,installer)
    else:
        certificate=json.loads(Path(signing_policy).read_bytes())['certificate']
        if (signed_installer.get('name')!=name or signed_installer.get('certificate')!={
                'sha1':certificate['sha1'],'sha256':certificate['sha256'],'subject':certificate['subject'],'issuer':certificate['issuer']}
                or signed_installer.get('file_digest')!='SHA256' or signed_installer.get('timestamp_digest')!='SHA256'
                or signed_installer.get('timestamp')!='RFC3161'):
            raise ValueError('Desktop transformed host does not match actual OA signature observation')
        _desktop_member(target_root,{key:signed_installer[key] for key in ('name','bytes','sha256')})
    if value['helper_receipt'].get('name')!='preparation-native-receipt.json':raise ValueError('Desktop helper receipt role differs')
    _desktop_member(root,value['helper_receipt'])
    if set(value['helpers'])!={'comfyui-hostfolder.dll','comfyui-prepare-instance.exe','desktop-uninstaller.exe'}:
        raise ValueError('Desktop helper role closure differs')
    for name,measured in value['helpers'].items():_desktop_member(root,dict(name=name,**measured))
    return value


def custom_desktop_binding(input_root,authenticated_receipt_pin,signing_policy,*,
                           expected_source,expected_run,expected_attempt,installer_root=None,source_root=None):
    """Receive final signed app/source closure from independently authenticated intake."""
    value=_desktop_receipt(input_root,'desktop-build-receipt.json',authenticated_receipt_pin,
        'desktop-final',signing_policy,source=expected_source,run=expected_run,attempt=expected_attempt)
    allowed={'schema','stage','version','source_policy','build_inputs','identity','source','run_id','run_attempt',
                     'host','native_files','desktop_files','signatures','installer','helper_receipt','helpers','source_pair'}
    if value['schema']==3:allowed.add('supplier_preserved')
    if BACKEND_HELPER in value.get('native_files',{}):allowed.add('helper_carrier')
    if set(value)!=allowed:
        raise ValueError('final Desktop receipt fields differ')
    if not re.fullmatch(r'OpenAbstractions-ComfyUI-[0-9]+\.[0-9]+\.[0-9]+-win-x64\.exe',value['installer'].get('name','')):
        raise ValueError('final Desktop installer name differs')
    _desktop_member(installer_root if installer_root is not None else input_root,value['installer'])
    pair=value['source_pair']
    if not pair.get('name','').endswith('-source.zip') or '/' in pair['name']:
        raise ValueError('final Desktop source pair name differs')
    if value['schema']==3:
        owner_path=Path(signing_policy).parent.parent/'scripts/app_pair_receipt.py'
        spec=importlib.util.spec_from_file_location('desktop_final_source_owner',owner_path)
        owner=importlib.util.module_from_spec(spec);exec(compile(owner_path.read_bytes(),str(owner_path),'exec'),owner.__dict__)
        owner.verify_archive_set(source_root if source_root is not None else input_root,pair,format='zip')
        if pair['bytes']>8<<30:raise ValueError('final Desktop source archive exceeds logical bound')
    else:_desktop_member(source_root if source_root is not None else input_root,pair)
    if value['helper_receipt'].get('name')!='preparation-native-receipt.json':raise ValueError('Desktop helper receipt role differs')
    _desktop_member(input_root,value['helper_receipt'])
    if set(value['helpers'])!={'comfyui-hostfolder.dll','comfyui-prepare-instance.exe','desktop-uninstaller.exe'}:
        raise ValueError('final Desktop helper closure differs')
    for name,measured in value['helpers'].items():_desktop_member(input_root,dict(name=name,**measured))
    return value


def desktop_source_policy():
    """The selected producer source owns policy; a build receipt cannot redefine it."""
    value=json.loads(DESKTOP_SOURCE.read_bytes())
    if set(value)!={'schema','upstream','identity','lock','patch','inputs','helpers','licensing'} or value['schema']!=1:
        raise ValueError('unsupported custom Desktop source policy')
    upstream=value['upstream']
    if upstream!={'repository':'https://github.com/Comfy-Org/Comfy-Desktop',
                  'commit':'5d19e86a8d14c799edd91968a19afa7bc8ac72ff','version':'1.1.8'}:
        raise ValueError('custom Desktop upstream source differs')
    licensing=value['licensing']
    if (not isinstance(licensing,dict) or set(licensing)!={'expression','file','bytes','sha256'}
            or licensing['expression']!='AGPL-3.0-or-later' or licensing['file']!='LICENSE'
            or type(licensing['bytes']) is not int or licensing['bytes']<=0
            or not re.fullmatch('[0-9a-f]{64}',str(licensing['sha256']))):
        raise ValueError('custom Desktop source licensing differs')
    patch=DESKTOP_SOURCE.parent/value['patch']['file']
    if patch.parent!=DESKTOP_SOURCE.parent or patch.name!='desktop-oa.patch' or patch.is_symlink() or patch.is_junction() or \
       pin(patch)['sha256']!=value['patch']['sha256']:
        raise ValueError('custom Desktop policy patch differs')
    if value['lock']!={'file':'pnpm-lock.yaml','sha256':'fc8b3249254e7f5e1da542a22e854e21421507548e69507c7b6208ababdd521e','bytes':245558}:
        raise ValueError('custom Desktop dependency lock differs')
    if set(value['helpers'])!={'desktop-nsis-sign.cjs'}:
        raise ValueError('custom Desktop build helper closure differs')
    for name,expected in value['helpers'].items():
        helper=DESKTOP_SOURCE.parent/name
        if helper.is_symlink() or helper.is_junction() or pin(helper)!=expected:
            raise ValueError('custom Desktop build helper changed')
    for name,row in value['inputs'].items():
        if not name or '\\' in name or name.startswith('/') or any(part in ('','.','..') for part in name.split('/')) or \
           set(row)!={'sha256','normalized_sha256','patched_sha256'} or \
           any(not re.fullmatch('[0-9a-f]{64}',digest) for digest in row.values()):
            raise ValueError('custom Desktop source input differs')
    return value,patch

def prepare_desktop_source(source,out,git='git',*,git_environment_owner=None):
    """Apply the exact reviewed patch to an exact, clean owned upstream checkout.

    This stage executes Git only. It runs no supplier lifecycle, compiler or app.
    The resulting receipt is source preparation, never binary/signing authority.
    """
    if source is None:raise ValueError('source preparation requires --source-root')
    source=Path(source).absolute();out=Path(out).absolute()
    if any(path.is_symlink() or path.is_junction() for path in (source,*source.parents,out,*out.parents)) or not source.is_dir() or out.exists():
        raise ValueError('source preparation requires a plain checkout and fresh output')
    policy,patch=desktop_source_policy()
    license_file=source/policy['licensing']['file']
    if license_file.is_symlink() or license_file.is_junction() or pin(license_file)!={key:policy['licensing'][key] for key in ('bytes','sha256')}:
        raise ValueError('Desktop source license differs before patch')
    if git_environment_owner is None:raise ValueError('source preparation requires the selected Git environment owner')
    owner_path=Path(git_environment_owner).absolute()
    if any(part.is_symlink() or part.is_junction() for part in (owner_path,*owner_path.parents)) or not owner_path.is_file():
        raise ValueError('selected Git environment owner is missing or linked')
    spec=importlib.util.spec_from_file_location('oa_desktop_git_environment',owner_path)
    owner=importlib.util.module_from_spec(spec)
    exec(compile(owner_path.read_bytes(),str(owner_path),'exec'),owner.__dict__)
    environment=owner.source_git_environment()
    command=[git,'-C',str(source)]
    lock=source/policy['lock']['file']
    if lock.is_symlink() or lock.is_junction() or pin(lock)!={key:policy['lock'][key] for key in ('sha256','bytes')}:
        raise ValueError('Desktop dependency lock differs before patch')
    for name,row in policy['inputs'].items():
        at=source
        for part in name.split('/'):
            at=at/part
            if at.is_symlink() or at.is_junction():raise ValueError('Desktop source crosses a link')
        data=at.read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(data).hexdigest()!=row['normalized_sha256']:
            raise ValueError('Desktop source differs before patch: '+name)
    def read(*args):
        return subprocess.check_output([*command,*args],env=environment,text=True).strip()
    if read('rev-parse','HEAD')!=policy['upstream']['commit'] or read('status','--porcelain','--untracked-files=all'):
        raise ValueError('Desktop source requires the exact clean pinned checkout')
    before=pin(DESKTOP_SOURCE),pin(patch)
    subprocess.run([*command,'apply','--check',str(patch)],env=environment,check=True)
    subprocess.run([*command,'apply',str(patch)],env=environment,check=True)
    for name,row in policy['inputs'].items():
        data=(source/name).read_bytes().replace(b'\r\n',b'\n')
        if hashlib.sha256(data).hexdigest()!=row['patched_sha256']:
            raise ValueError('Desktop patched source differs: '+name)
    if before!=(pin(DESKTOP_SOURCE),pin(patch)):raise ValueError('Desktop source policy changed')
    out.mkdir(parents=True)
    receipt=dict(schema=1,source=policy['upstream'],policy=before[0],patch=before[1],
                 inputs=policy['inputs'],git_environment=pin(owner_path),
                 authority='Source preparation only; no binary, dependency or signing admission.')
    (out/'desktop-source-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    return 0

def source_roles(shared_app_builder):
    """Source policy only; actual compiled tables and signatures are received elsewhere."""
    shared=Path(shared_app_builder)
    namespace='{http://wixtoolset.org/schemas/v4/wxs}'
    tree=ET.fromstring((shared/'extension.wxs').read_bytes())
    actions=[row for row in tree.iter(namespace+'CustomAction') if row.get('DllEntry')=='FindHostFolder']
    if len(actions)!=1:raise ValueError('extension owner must declare one host-folder native action')
    identity=actions[0].get('BinaryRef')
    binaries=[row for row in tree.iter(namespace+'Binary') if row.get('Id')==identity]
    if len(binaries)!=1 or binaries[0].get('SourceFile')!='$(var.HostFolderDll)':raise ValueError('host action source policy differs')
    return ((('Binary',identity,'comfyui-hostfolder.dll'),),(),('comfyui-prepare-instance.exe',))

def pin(path):
    data=Path(path).read_bytes()
    return dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())

def binding(path,input_root,*,authenticated_receipt_pin=None,signing_policy=None,historical=False,
            expected_source=None,expected_run=None,expected_attempt=None):
    path=Path(path).absolute();input_root=Path(input_root).absolute()
    if '..' in path.parts or '..' in input_root.parts or path.name!='desktop-host.json' or input_root.is_symlink() or input_root.is_junction():
        raise ValueError('desktop binding must be the selected plain desktop-host.json beneath input root')
    if any(at.is_symlink() or at.is_junction() for at in (path,*path.parents)):
        raise ValueError('desktop binding crosses a link')
    suffix=_boundary.relative_beneath_existing(path,input_root)
    at=input_root
    for part in suffix.parts:
        at=at/part
        if at.is_symlink() or at.is_junction():raise ValueError('desktop binding crosses a link')
    document=json.loads(path.read_text(encoding='utf-8'))
    if document.get('schema')==2:
        if pin(path)!=pin(HERE/'desktop-host.json'):
            raise ValueError('custom Desktop policy differs from selected source')
        if signing_policy is None:raise ValueError('custom Desktop requires selected signing policy')
        built=custom_desktop_binding(input_root,authenticated_receipt_pin,signing_policy,
            expected_source=expected_source,expected_run=expected_run,expected_attempt=expected_attempt)
        host=dict(built['host'],exe=dict(built['host']['exe'],name=Path(built['host']['exe']['name']).name))
        return dict(schema=2,official_installer=built['installer'],host=host,custom_build=built)
    if not historical:
        raise ValueError('historical supplier schema1 cannot satisfy the current custom Desktop policy')
    if set(document)!={'schema','official_installer','host'} or document['schema']!=1:
        raise ValueError('unsupported desktop binding')
    installer=document['official_installer'];host=document['host']
    if set(installer)!={'name','sha256','bytes','source_url'} or set(host)!={'arp_id','version','exe','publisher'}:
        raise ValueError('desktop binding fields differ')
    if set(host['exe'])!={'name','sha256','bytes'} or set(host['publisher'])!={'subject','thumbprint'}:
        raise ValueError('desktop executable/publisher facts differ')
    for item in (installer,host['exe']):
        if not isinstance(item['name'],str) or not re.fullmatch(r'[A-Za-z0-9 ._-]+\.exe',item['name']) or \
           not re.fullmatch(r'[0-9a-f]{64}',item['sha256']) or type(item['bytes']) is not int or item['bytes']<=0:
            raise ValueError('desktop binding lacks exact executable bytes')
    if not installer['source_url'].startswith('https://') or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',host['arp_id']) or \
       host['version']!='1.1.8' or host['exe']['name']!='Comfy Desktop.exe' or not host['publisher']['subject'] or not re.fullmatch(r'[0-9A-F]{40}',host['publisher']['thumbprint']):
        raise ValueError('desktop binding lacks supported host identity')
    return document

def input_paths(args,shared,out):
    if _boundary_path.is_symlink() or _boundary_path.is_junction() or not _boundary_path.is_file():
        raise ValueError('producer boundary helper must be a plain source file')
    if _boundary_path.read_bytes()!=_boundary_bytes:
        raise ValueError('producer boundary helper changed after load')
    return {'shared:native_windows.h':native_identity_source(shared),'desktop_binding':args.desktop_binding,'signed_desktop_receipt':args.signed_desktop_receipt,
            'source:git_env.py':shared.parents[1]/'scripts/git_env.py',
            'received_inputs':args.input_receipt,'source:desktop-host.json':HERE/'desktop-host.json',
            'source:desktop-source.json':DESKTOP_SOURCE,'source:desktop-oa.patch':DESKTOP_SOURCE.parent/'desktop-oa.patch',
            'source:backend-amd-v1.json':BACKEND_SOURCE,'source:desktop_backend.cjs':HERE.parent/'desktop_backend.cjs',
            'source:desktop-nsis-sign.cjs':DESKTOP_SOURCE.parent/'desktop-nsis-sign.cjs',
            **{'source:'+name:HERE/name for name in SOURCES},
            **{'shared:'+name:shared/name for name in SHARED},
            **{'generated:'+name:out/name for name in ('desktop_inputs.h','native_version.h')}}

def fixture_removal_policy():
    """Describe fixture-only cleanup; the receiving owner must prove new ownership."""
    return {'profile': 'nsis-current-user', 'basename': 'Uninstall OpenAbstractions ComfyUI.exe'}


def nsis_package(args,out,*,mode,unsigned=None,signed=None,iteration='package'):
    """Run the pinned upstream NSIS owner with the fixed source-owned sign hook.

    Emission executes the generated upstream build stub. This declared build
    effect requires producer admission; source checks never invoke this method.
    """
    tools=desktop_tool_inputs(args.input_root);source=Path(args.input_root)/'upstream/desktop'
    policy,patch=desktop_source_policy()
    states={hashlib.sha256((source/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==row['normalized_sha256']
            for name,row in policy['inputs'].items()}
    if states=={True}:
        prepare_desktop_source(source,out/'nsis-source-preparation',args.git,
            git_environment_owner=args.shared_app_builder.absolute().parents[1]/'scripts/git_env.py')
    if not (source/'node_modules').exists():
        _copy_tool_tree(tools['pnpm'],source/'node_modules',prefix='node_modules/')
    _verify_tool_tree(tools['pnpm'],source/'node_modules',prefix='node_modules/')
    received=json.loads(args.input_receipt.read_bytes())
    icon=Path(args.input_root)/'oa-comfyui.ico'
    if pin(icon)!=received.get('build_inputs',{}).get('oa-comfyui.ico'):
        raise ValueError('NSIS branding differs from authenticated producer input')
    resources=source/'resources'
    (resources/'oa-comfyui.ico').write_bytes(icon.read_bytes())
    (resources/'vc_redist.x64.exe').write_bytes(tools['vc_redist']['entry'].read_bytes())
    for name,row in policy['inputs'].items():
        if hashlib.sha256((source/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=row['patched_sha256']:
            raise ValueError('NSIS requires the exact prepared Desktop source')
    destination=out/('nsis-'+iteration)
    if destination.exists():raise ValueError('NSIS output must be fresh')
    destination.mkdir()
    recipe=out/'nsis-recipe.json'
    recipe_value=dict(schema=1,source_policy=pin(DESKTOP_SOURCE),desktop=pin(out/'desktop-signed-receipt.json'),
        version=args.version,hook=policy['helpers']['desktop-nsis-sign.cjs'],
        builder='26.8.1',target='nsis',architecture='x64')
    raw=(json.dumps(recipe_value,sort_keys=True)+'\n').encode()
    if recipe.exists() and recipe.read_bytes()!=raw:raise ValueError('NSIS recipe changed')
    recipe.write_bytes(raw)
    current_source=getattr(args,'package_source',None) or os.environ.get('CANDIDATE_SHA')
    current_run=getattr(args,'package_run',None) or int(os.environ.get('GITHUB_RUN_ID','0'))
    current_attempt=getattr(args,'package_attempt',None) or int(os.environ.get('GITHUB_RUN_ATTEMPT','0'))
    if (not re.fullmatch('[0-9a-f]{40}',str(current_source)) or type(current_run) is not int or current_run<=0
            or type(current_attempt) is not int or current_attempt<=0):raise ValueError('NSIS build requires its actual producer identity')
    installer='OpenAbstractions-ComfyUI-'+policy['upstream']['version']+'-win-x64.exe'
    context=dict(schema=1,mode=mode,source=current_source,run_id=current_run,run_attempt=current_attempt,
        recipe=pin(recipe),output_dir=str(destination.absolute()),installer_name=installer,
        uninstaller_name=installer[:-3]+'__uninstaller.exe')
    if mode=='reuse':
        context['unsigned']=unsigned
        target=destination/'oa-signed/desktop-uninstaller-signed.exe';target.parent.mkdir()
        target.write_bytes(Path(signed).read_bytes())
        context['signed']=dict(name='oa-signed/desktop-uninstaller-signed.exe',**pin(target))
    context_path=destination/'nsis-context.json';context_path.write_text(json.dumps(context),encoding='utf-8')
    config=destination/'builder.json'
    config.write_text(json.dumps(dict(extends=str(source/'electron-builder.yml'),
        directories=dict(output=str(destination)),npmRebuild=False,nodeGypRebuild=False,publish=None,
        artifactName=installer,win=dict(signtoolOptions=dict(signingHashAlgorithms=['sha256'],
            sign=str(DESKTOP_SOURCE.parent/'desktop-nsis-sign.cjs'))))),encoding='utf-8')
    environment=desktop_environment(out,tools)
    environment.update(CANDIDATE_SHA=str(current_source),GITHUB_RUN_ID=str(current_run),GITHUB_RUN_ATTEMPT=str(current_attempt),
        OA_NSIS_CONTEXT=str(context_path),OA_NSIS_CONTEXT_SHA256=pin(context_path)['sha256'],
        OA_DESKTOP_RECIPE_SHA256=pin(recipe)['sha256'])
    pnpm=([str(tools['node']['entry']),str(tools['pnpm']['entry'])] if tools['pnpm']['entry'].suffix=='.cjs' else [str(tools['pnpm']['entry'])])
    before=desktop_native_inventory(out/'desktop',all_files=True)
    result=subprocess.run([*pnpm,'exec','electron-builder','--prepackaged',str(out/'desktop'),
        '--win','nsis','--x64','--publish','never','--config',str(config)],cwd=source,env=environment,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if desktop_native_inventory(out/'desktop',all_files=True)!=before:raise ValueError('NSIS mutated the signed Desktop complete closure')
    _verify_tool_tree(tools['pnpm'],source/'node_modules',prefix='node_modules/')
    if mode=='emit':
        export=destination/'nsis-uninstaller-export.json'
        if result.returncode==0 or not export.is_file():raise ValueError('NSIS did not stop at fixed uninstaller export')
        value=json.loads(export.read_bytes())
        if (set(value)!={'schema','source','run_id','run_attempt','recipe','unsigned','generated_name'}
                or value['schema']!=1 or any(value[key]!=context[key] for key in ('source','run_id','run_attempt','recipe'))
                or value['generated_name']!=context['uninstaller_name']):raise ValueError('NSIS export provenance differs')
        _desktop_member(destination,dict(name='desktop-uninstaller-unsigned.exe',**value['unsigned']))
        return destination/'desktop-uninstaller-unsigned.exe'
    if result.returncode:raise ValueError('source-owned NSIS packaging failed')
    return _desktop_member(destination,dict(name=installer,**pin(destination/installer)))


def prepare_uninstaller(args,out):
    first=nsis_package(args,out,mode='emit',iteration='emit-a')
    second=nsis_package(args,out,mode='emit',iteration='emit-b')
    if pin(first)!=pin(second):raise ValueError('NSIS uninstaller recreation is nondeterministic; preserve both outputs')
    target=out/'desktop-uninstaller.exe';target.write_bytes(first.read_bytes())
    return pin(target)


def desktop_embedded_integration(root,declared,shared):
    """Read embedded-wrapper evidence from the admitted complete Desktop tree."""
    policy=custom_desktop_policy()
    if (declared.get('custom_build',{}).get('schema')!=3
            or declared['custom_build']['source_policy']!=policy['source_policy']
            or declared['custom_build']['build_inputs']!=policy['build_inputs']):
        raise ValueError('embedded integration requires the source-selected custom Desktop')
    base=Path(root)/'desktop/resources/oa-backend'
    source=shared.parents[1]/'scripts/app_pair_receipt.py'
    spec=importlib.util.spec_from_file_location('oa_desktop_embedded_pair_owner',source)
    owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner)
    resource=owner.validate_backend_resource(json.loads((base/'desktop-backend.json').read_bytes()))
    expected=declared['custom_build']['build_inputs']['backend']
    if resource['provenance']['input_descriptor']!=dict(name='backend-inputs.json',**expected):
        raise ValueError('embedded backend input differs from selected Desktop build')
    selected=resource['provenance']['manifest'];path=base/selected['name']
    if path.is_symlink() or path.is_junction() or pin(path)!={key:selected[key] for key in ('bytes','sha256')}:
        raise ValueError('embedded backend manifest differs from signed Desktop resource')
    manifest=json.loads(path.read_bytes())
    value=dict(schema=1,distribution='oa-custom-desktop',source_policy=declared['custom_build']['source_policy'],
        oa_integration=manifest['oa_integration'],backend_manifest=selected)
    return owner.validate_embedded_integration(value,source_policy=declared['custom_build']['source_policy'],
        oa_integration=manifest['oa_integration'],backend_manifest=selected,manifest=manifest)


def package(args,shared,out,declared):
    preparation_receipt=out/'preparation-native-receipt.json'
    receipt_bytes=preparation_receipt.read_bytes();receipt_pin=dict(bytes=len(receipt_bytes),sha256=hashlib.sha256(receipt_bytes).hexdigest())
    receipt=json.loads(receipt_bytes)
    paths=input_paths(args,shared,out)
    if receipt['version']!=args.version or receipt['desktop_binding']!=pin(args.desktop_binding) or receipt['inputs']!={key:pin(path) for key,path in paths.items()}:
        raise ValueError('prepared native source closure differs')
    native={}
    for role in ('action','preparation','uninstaller'):
        path=getattr(args,role)
        if path is not None:path=path.absolute();setattr(args,role,path)
        expected=dict(sha256=getattr(args,role+'_sha256'),bytes=getattr(args,role+'_bytes'))
        expected_name={'action':'comfyui-hostfolder.dll','preparation':'comfyui-prepare-instance.exe','uninstaller':'desktop-uninstaller.exe'}[role]
        if path is None or path.name!=expected_name or path.is_symlink() or path.is_junction() or pin(path)!=expected:raise ValueError('missing or changed final signed native '+role)
        native[role]=dict(expected,name=path.name)
    if args.extension is None or args.include_root is None or args.input_receipt is None:raise ValueError('package requires explicit descriptor, SDK include root and existing received input receipt')
    if (not re.fullmatch('[0-9a-f]{40}',str(args.package_source)) or type(args.package_run) is not int or args.package_run<=0
            or type(args.package_attempt) is not int or args.package_attempt<=0):raise ValueError('package requires actual producer identity')
    unsigned=pin(out/'nsis-emit-a/desktop-uninstaller-unsigned.exe')
    if pin(out/'nsis-emit-b/desktop-uninstaller-unsigned.exe')!=unsigned:
        raise ValueError('prepared uninstaller recreation proof differs')
    desktop=nsis_package(args,out,mode='reuse',unsigned=unsigned,signed=args.uninstaller)
    official=dict(name=desktop.name,**pin(desktop))
    source_roles(shared)
    package_inputs={name:pin(path) for name,path in [('descriptor',args.extension),('received_inputs',args.input_receipt),('official_installer',desktop)]}
    module_path=shared/'build_extension.py';spec=importlib.util.spec_from_file_location('oa_comfy_extension_owner',module_path)
    owner=importlib.util.module_from_spec(spec);exec(compile(module_path.read_bytes(),str(module_path),'exec'),owner.__dict__)
    extension=owner.read_extension(args.extension)
    if extension['id']!='comfyui':raise ValueError('package descriptor must select ComfyUI')
    destination=out/'package'
    arguments=['--extension',str(args.extension),'--publisher',args.publisher,'--version',args.version,'--out',str(destination),
               '--include-root',str(args.include_root),'--input-receipt',str(args.input_receipt),'--action-dll',str(args.action),
               '--action-sha256',args.action_sha256,'--action-bytes',str(args.action_bytes),'--wix',args.wix]
    modern=declared['custom_build']['schema']==3
    if modern:
        embedded=desktop_embedded_integration(out,declared,shared)
        destination.mkdir(parents=True)
        app=dict(id='comfyui',title=extension['title'],embedded_integration=embedded)
        (destination/'bundle-app.json').write_text(json.dumps(app,indent=2)+'\n',encoding='utf-8')
        value=dict(schema=3,id='comfyui',version=args.version,platform='windows/amd64',
            status='Embedded wrapper; source/signature/runtime qualification pending',
            descriptor=pin(args.extension),embedded_integration=embedded,
            received_input_receipt=pin(args.input_receipt))
        (destination/'installer-receipt.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    elif owner.main(arguments):raise ValueError('extension package owner failed')
    final_desktop=destination/desktop.name
    final_desktop.write_bytes(desktop.read_bytes())
    if pin(final_desktop)!=pin(desktop):raise ValueError('packaged outer host changed during copy')
    desktop=final_desktop
    if receipt['inputs']!={key:pin(path) for key,path in paths.items()} or any(pin(getattr(args,role))!={key:value for key,value in native[role].items() if key!='name'} for role in native):
        raise ValueError('package source/native input changed')
    if pin(preparation_receipt)!=receipt_pin or package_inputs!={name:pin(path) for name,path in [('descriptor',args.extension),('received_inputs',args.input_receipt),('official_installer',desktop)]}:raise ValueError('package received input changed')
    app_path=destination/'bundle-app.json';app=json.loads(app_path.read_text())
    if not modern:
        msi=Path(app['file'])
        if not msi.is_absolute():msi=destination/msi
        msi=msi.absolute()
        if pin(msi)!={key:app[key] for key in ('sha256','bytes')}:raise ValueError('packaged MSI differs from bundle pin')
        app['file']=str(msi)
    app['host']=dict(file=str(desktop),sha256=official['sha256'],bytes=official['bytes'],
                     arp_id=declared['host']['arp_id'],arp_version=declared['host']['version'],arp_win64=True,
                     preparation=dict(file=str(args.preparation),sha256=args.preparation_sha256,bytes=args.preparation_bytes))
    if 'source_url' in official:app['host']['download_url']=official['source_url']
    app_path.write_text(json.dumps(app,indent=2)+'\n',encoding='utf-8')
    packaged=destination/'installer-receipt.json';value=json.loads(packaged.read_text())
    value.update(prepared_native=receipt['outputs'],consumed_native=native,preparation_source=receipt_pin,
                 desktop_binding=pin(args.desktop_binding),package_inputs=package_inputs,bundle_app_sha256=pin(app_path)['sha256'])
    if not modern:value['fixture_removal']=fixture_removal_policy()
    packaged.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
    built=declared['custom_build']
    package_receipt={key:built[key] for key in ('schema','version','source_policy','build_inputs','identity','host','native_files','desktop_files','signatures')}
    if built['schema']==3:package_receipt['supplier_preserved']=built['supplier_preserved']
    if 'helper_carrier' in built:package_receipt['helper_carrier']=built['helper_carrier']
    package_receipt.update(stage='desktop-packaged-unsigned',source=args.package_source,run_id=args.package_run,
        run_attempt=args.package_attempt,installer=official,helper_receipt=dict(name='preparation-native-receipt.json',**receipt_pin),
        helpers={row['name']:{key:row[key] for key in ('sha256','bytes')} for row in native.values()})
    (destination/'desktop-package-receipt.json').write_text(json.dumps(package_receipt,indent=2)+'\n',encoding='utf-8')
    return 0

def native_identity_source(shared):
    source=shared/'build_extension.py'
    spec=importlib.util.spec_from_file_location('oa_comfy_shared_identity_owner',source)
    owner=importlib.util.module_from_spec(spec)
    exec(compile(source.read_bytes(),str(source),'exec'),owner.__dict__)
    return owner.native_identity_source()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=('source','desktop-prepare','prepare','package','local-native'),default='prepare')
    parser.add_argument('--desktop-binding',type=Path)
    parser.add_argument('--input-root',type=Path)
    parser.add_argument('--shared-app-builder',type=Path)
    parser.add_argument('--source-root',type=Path)
    parser.add_argument('--git',default='git')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--version',required=True,help='Immutable selected OA release version')
    parser.add_argument('--cl',default='cl.exe');parser.add_argument('--rc',default='rc.exe')
    parser.add_argument('--cvtres',type=Path)
    parser.add_argument('--backend-manifest',type=Path)
    parser.add_argument('--control-manifest',type=Path)
    parser.add_argument('--private-sdk-receipt',type=Path)
    parser.add_argument('--private-source-commit')
    parser.add_argument('--extension',type=Path);parser.add_argument('--include-root',type=Path)
    parser.add_argument('--input-receipt',type=Path);parser.add_argument('--publisher',default='OpenAbstractions');parser.add_argument('--wix',default='wix')
    parser.add_argument('--signed-desktop-receipt',type=Path)
    parser.add_argument('--signed-desktop-sha256');parser.add_argument('--signed-desktop-bytes',type=int)
    parser.add_argument('--producer-source');parser.add_argument('--producer-run',type=int);parser.add_argument('--producer-attempt',type=int)
    parser.add_argument('--package-source');parser.add_argument('--package-run',type=int);parser.add_argument('--package-attempt',type=int)
    for role in ('action','preparation','uninstaller'):
        parser.add_argument('--'+role,type=Path);parser.add_argument('--'+role+'-sha256');parser.add_argument('--'+role+'-bytes',type=int)
    if argv is None and len(sys.argv)==1:parser.print_help();return 0
    args=parser.parse_args(argv)
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',args.version):raise ValueError('invalid native version')
    if args.phase=='local-native':
        if (not all((args.shared_app_builder,args.source_root,args.private_sdk_receipt,args.private_source_commit,args.backend_manifest,args.control_manifest,args.cvtres))
                or any((args.producer_source,args.producer_run,args.producer_attempt,args.package_source,args.package_run,args.package_attempt,
                        args.signed_desktop_receipt,args.desktop_binding,args.input_root,args.input_receipt))):
            raise ValueError('local native preview requires exact private source/SDK/resources and no public producer/signature context')
        out=args.out.absolute()
        if out.exists() or out.is_symlink() or out.is_junction():raise ValueError('local native preview output must be fresh')
        out.mkdir(parents=True)
        result=build_native_host({},args.shared_app_builder.absolute(),out,out,
            backend_manifest=args.backend_manifest,control_manifest=args.control_manifest,rc=args.rc,cvtres=args.cvtres,
            private_workspace=args.source_root.absolute(),private_sdk_receipt=args.private_sdk_receipt,
            private_source_commit=args.private_source_commit)
        (out/'native-host-receipt.json').write_text(json.dumps(result,sort_keys=True)+'\n',encoding='utf-8')
        print(json.dumps(result,sort_keys=True))
        return 0
    if args.phase=='source':
        if args.shared_app_builder is None:raise ValueError('source preparation requires --shared-app-builder')
        return prepare_desktop_source(args.source_root,args.out,args.git,
            git_environment_owner=args.shared_app_builder.absolute().parents[1]/'scripts/git_env.py')
    if args.desktop_binding is None or args.input_root is None or args.shared_app_builder is None:
        raise ValueError('native preparation/package requires selected binding, input root and shared app builder')
    shared=args.shared_app_builder.absolute();out=args.out.absolute()
    if any(path.is_symlink() or path.is_junction() for path in (shared,*shared.parents,out,*out.parents)):
        raise ValueError('native build paths cannot traverse links')
    if args.phase=='desktop-prepare':return desktop_prepare(args,shared,out)
    if args.phase=='prepare' and any((out/name).exists() for name in ('preparation-native-receipt.json','comfyui-prepare-instance.exe','comfyui-hostfolder.dll')):
        raise ValueError('native preparation requires fresh output files')
    for name in SHARED:
        if not (shared/name).is_file() or (shared/name).is_symlink():raise ValueError('missing selected shared header '+name)
    def conflicts():
        for parent in (HERE,out):
            for name in ('user_token.h','hostfolder_io.h','native_windows.h'):
                if (parent/name).exists():raise ValueError('conflicting selected shared header '+name)
    identity=native_identity_source(shared)
    binding_pin=pin(args.desktop_binding)
    if args.input_receipt is None:raise ValueError('custom Desktop requires the authenticated current producer input receipt')
    if pin(args.desktop_binding)!=pin(HERE/'desktop-host.json'):raise ValueError('Desktop policy differs from selected source')
    built=signed_desktop(args,shared,out)
    declared=dict(schema=2,host=dict(built['host'],exe=dict(built['host']['exe'],name=Path(built['host']['exe']['name']).name)),custom_build=built)
    if pin(args.desktop_binding)!=binding_pin:raise ValueError('desktop binding changed while read')
    if args.phase=='package':return package(args,shared,out,declared)
    conflicts()
    prepare_uninstaller(args,out)
    paths={key:path for key,path in input_paths(args,shared,out).items() if not key.startswith('generated:')}
    paths['shared:native_windows.h']=identity
    inputs={key:pin(path) for key,path in paths.items()}
    host=declared['host'];exe=host['exe']
    header='/* Generated only from the selected host binding; input JSON is data, not authority. */\n'
    for name,value in [('OA_COMFY_ARP','Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\'+host['arp_id']),
                       ('OA_COMFY_VERSION',host['version']),('OA_COMFY_EXE',exe['name']),
                       ('OA_COMFY_RECORDS',custom_desktop_policy()['identity']['records'].replace('/','\\'))]:
        header+='#define '+name+' L'+json.dumps(value,ensure_ascii=True)+'\n'
    header+='#define OA_COMFY_EXE_SHA256 '+json.dumps(exe['sha256'])+'\n#define OA_COMFY_EXE_BYTES '+str(exe['bytes'])+'ULL\n'
    header+='#define OA_COMFY_ALLOW_LEGACY 0\n'
    out.mkdir(parents=True,exist_ok=True);generated=out/'desktop_inputs.h';generated.write_text(header,encoding='utf-8',newline='\n')
    version=out/'native_version.h'
    version.write_text('#define OA_FILE_VERSION '+args.version.replace('.',',')+',0\n#define OA_VERSION_STRING "'+args.version+'\\0"\n',encoding='utf-8')
    paths['generated:desktop_inputs.h']=generated;inputs['generated:desktop_inputs.h']=pin(generated)
    paths['generated:native_version.h']=version;inputs['generated:native_version.h']=pin(version)
    flags=['/nologo','/std:c17','/W4','/WX','/O2','/MT','/GS','/guard:cf','/Brepro','/I'+str(identity.parent),'/I'+str(shared),'/I'+str(out),'/FI'+str(generated)]
    conflicts()
    libraries=['msi.lib','shell32.lib','shlwapi.lib','ole32.lib','uuid.lib','bcrypt.lib','advapi32.lib','user32.lib','wintrust.lib']
    for source,name,dll in [('prepare_instance.c','comfyui-prepare-instance.exe',False),('hostfolder_action.c','comfyui-hostfolder.dll',True)]:
        conflicts()
        resource=out/(name+'.res')
        subprocess.run([args.rc,'/nologo','/I'+str(out),'/dOA_NATIVE_FILE_TYPE='+('0x2L' if dll else '0x1L'),'/fo',str(resource),str(HERE/'prepare_instance.rc')],check=True,cwd=out)
        command=[args.cl,*flags,*(['/LD'] if dll else []),'/Fo'+str(out/(name+'.obj')),'/Fe'+str(out/name),str(HERE/source)]
        command.append(str(resource))
        command+=['/link','/guard:cf','/DYNAMICBASE','/NXCOMPAT',*libraries]
        if not dll:command+=['/SUBSYSTEM:WINDOWS','/MANIFEST:EMBED','/MANIFESTINPUT:'+str(HERE/'prepare_instance.manifest')]
        subprocess.run(command,check=True,cwd=out)
    conflicts()
    for key,expected in inputs.items():
        if pin(paths[key])!=expected:raise ValueError('native preparation input changed: '+key)
    receipt=dict(schema=1,version=args.version,desktop_binding=pin(args.desktop_binding),inputs=inputs,
                 outputs={name:pin(out/name) for name in ('comfyui-prepare-instance.exe','comfyui-hostfolder.dll','desktop-uninstaller.exe')},
                 authority='Source build receipt only; release owner authenticates selected inputs and signs final native outputs.')
    (out/'preparation-native-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    return 0

if __name__=='__main__':raise SystemExit(main())
