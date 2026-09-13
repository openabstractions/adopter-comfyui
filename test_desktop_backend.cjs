'use strict';

// Synthetic pinned SDK and archive fixtures exercise the Electron receiver.
// No native addon, archive executable, network or product process is launched.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const crypto=require('node:crypto');
const Module=require('node:module');
const vm=require('node:vm');
const {EventEmitter}=require('node:events');
const {PassThrough}=require('node:stream');
const {pathToFileURL}=require('node:url');
const cp=require('node:child_process');
const net=require('node:net');
const http=require('node:http');
const https=require('node:https');
const dgram=require('node:dgram');
const physical=[];
for (const owner of [cp,net,http,https]) for (const name of ['spawn','spawnSync','exec','execSync','execFile','execFileSync','fork','connect','createConnection','request','get']) {
  if (typeof owner[name]==='function') owner[name]=(...args)=>{physical.push(name);throw Error('unexpected physical '+name);};
}
for (const [owner,name] of [[process,'dlopen'],[globalThis,'fetch'],[net.Server.prototype,'listen'],[dgram,'createSocket']]) {
  owner[name]=(...args)=>{physical.push(name);throw Error('unexpected physical '+name);};
}
const digest=raw=>crypto.createHash('sha256').update(raw).digest('hex');
const pinned=(name,raw)=>({name,bytes:Buffer.byteLength(raw),sha256:digest(raw)});
const clone=value=>structuredClone(value);
let sequence=0;

test('actual native environment receiver accepts Windows case spelling and refuses a different directory',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'desktop_backend.cjs'),'utf8');
  const canonical=source.match(/function canonical\(p\) \{[^\n]+\}/)[0];
  const start=source.indexOf('async function archiveEnvironment('),end=source.indexOf('\nasync function archiveChild(',start);
  const actual=source.slice(start,end);assert.ok(start>=0&&end>start);
  const temporary='c:\\Owned\\Workspace\\.oa-state';
  let response={SystemRoot:'C:\\Windows',windir:'C:\\Windows',SystemDrive:'C:',PATH:'C:\\Windows\\System32',
    TEMP:'C:\\OWNED\\workspace\\.OA-STATE',TMP:'C:\\owned\\WORKSPACE\\.oa-state'};
  const receive=vm.runInNewContext(canonical+'\n'+actual+';archiveEnvironment',{path,process:{platform:'win32'},
    fail:message=>{throw Error(message);},closed:(value,keys)=>{assert.deepEqual(Object.keys(value).sort(),Array.from(keys).sort());return value;},
    boundedNativeChild:async()=>JSON.stringify(response)});
  assert.equal((await receive({directory:'C:\\signed'},temporary,'c:\\Owned\\Workspace',{})).TEMP,response.TEMP);
  for(const key of ['TEMP','TMP']){
    const original=response[key];response[key]='C:\\Owned\\foreign';
    await assert.rejects(receive({directory:'C:\\signed'},temporary,'c:\\Owned\\Workspace',{}),/differs from fixed/);
    response[key]=original;
  }
});

test('actual Desktop setup caller passes fresh workspace roles and awaits durable work before continuing',async()=>{
  const patch=fs.readFileSync(path.join(__dirname,'upstream/desktop-oa.patch'),'utf8');
  const added=patch.split(/\r?\n/).filter(line=>line.startsWith('+')&&!line.startsWith('+++')).map(line=>line.slice(1)).join('\n');
  const start=added.indexOf('  // Existing OA jobs own transfers;');
  const end=added.indexOf('\n  })',start)+6;assert.ok(start>=0&&end>start);
  const block=added.slice(start,end).replace('(work: unknown)','(work)')
    .replace('(value: { percent: number; status: string })','(value)');
  const backend={variant:'win-amd'},installation={id:'inst-fixed',variant:'win-amd',installPath:'C:/owned/workspace',oaBackendWork:null};
  const sdkPin={name:'sdk.json',bytes:7,sha256:'a'.repeat(64)},events=[];
  let release,received;const persisted=new Promise(resolve=>{release=resolve;});
  const run=vm.runInNewContext('(async()=>{'+block+'})',{path,process:{resourcesPath:'C:/signed/resources'},installation,
    getOaBackend:()=>backend,fs:{lstatSync:()=>({isSymbolicLink:()=>false}),statSync:()=>({size:100}),readFileSync:()=>JSON.stringify(sdkPin)},
    tools:{signal:null,update:async value=>{events.push('saving');await persisted;events.push('saved');assert.equal(value.oaBackendWork.marker,'fixed');},sendProgress:()=>{}},
    createRequire:()=>()=>({installBackend:async options=>{received=options;await options.saveWork({marker:'fixed'});events.push('bridge-complete');}})});
  const pending=run();await Promise.resolve();assert.deepEqual(events,['saving']);
  assert.equal(received.target,path.join(installation.installPath,'backend'));
  assert.equal(received.cacheRoot,path.join(installation.installPath,'.oa-state'));
  assert.equal(received.resource,backend);assert.equal(received.work,null);
  assert.deepEqual(JSON.parse(JSON.stringify(received.sdkPin)),sdkPin);
  release();await pending;assert.deepEqual(events,['saving','saved','bridge-complete']);
});

test('actual Desktop spawn owner keeps helper stdin open and preserves explicit empty environment',()=>{
  const patch=fs.readFileSync(path.join(__dirname,'upstream/desktop-oa.patch'),'utf8');
  const section=patch.split('--- a/src/main/lib/process.ts\n')[1];assert.ok(section);
  const body=section.split('--- a/')[0].split('\n').filter(line=>(line.startsWith(' ')||line.startsWith('+'))&&!line.startsWith('+++')).map(line=>line.slice(1)).join('\n');
  const actual=body.match(/export function spawnProcess\([\s\S]*?\n\): ChildProcess \{[\s\S]*?\n\}/)?.[0];assert.ok(actual);
  const js=actual.replace(/export function spawnProcess\([\s\S]*?\n\): ChildProcess \{/,'function spawnProcess(cmd,args,cwd,env,options) {');
  let received;const ambient={SystemRoot:'Z:\\foreign',PYTHONPATH:'foreign'};
  const spawn=vm.runInNewContext(js+';spawnProcess',{process:{platform:'win32',env:ambient},spawn:(...args)=>{received=args;return {};}});
  const explicit={};spawn('fixed-helper.exe',['run-backend'],'C:/owned',explicit,{controlledInput:true});
  assert.deepEqual(Array.from(received[2].stdio),['pipe','pipe','pipe']);assert.equal(received[2].env,explicit);
  spawn('stock.exe',[],'C:/stock');assert.deepEqual(Array.from(received[2].stdio),['ignore','pipe','pipe']);assert.equal(received[2].env,ambient);
  const callers=patch.split('\n').filter(line=>line.startsWith('+')&&line.includes('controlledInput: inst.'));
  assert.equal(callers.length,2);
  for(const line of callers){
    const expression=line.slice(line.indexOf(':')+1).trim();
    for(const [sourceId,adopted,expected] of [['standalone',false,true],['standalone',true,false],['other',false,false]]){
      assert.equal(vm.runInNewContext(expression,{inst:{sourceId,adopted}}),expected);
    }
  }
});

test('actual patched launch manifest reader accepts authenticated metadata through 16 MiB',()=>{
  const patch=fs.readFileSync(path.join(__dirname,'upstream/desktop-oa.patch'),'utf8');
  const added=patch.split(/\r?\n/).filter(line=>line.startsWith('+')&&!line.startsWith('+++')).map(line=>line.slice(1)).join('\n');
  const start=added.indexOf('    let records: unknown');
  const end=added.indexOf("    if (typeof records !== 'string'",start);
  assert.ok(start>=0&&end>start);
  // Erase only the supplier's two TypeScript type annotations. The actual
  // read, bound, JSON parse, catch and records predicate execute unchanged.
  const predicate=added.slice(end,added.indexOf('\n',end));
  const block=added.slice(start,end).replace('let records: unknown','let records')
    .replace(/ as \{\n\s*private_python_seal\?: \{ records_sha256\?: unknown \}\n\s*\}/,'');
  assert.ok(!block.includes(': unknown')&&!block.includes(' as {'));
  const code='(()=>{'+block+'\n'+predicate+'\nreturn records;})()';
  for(const bytes of [(1<<20)+1,13<<20,16<<20,(16<<20)+1]){
    let reads=0;
    const value={private_python_seal:{records_sha256:'a'.repeat(64)},padding:''};
    value.padding='x'.repeat(bytes-Buffer.byteLength(JSON.stringify(value)));
    const raw=JSON.stringify(value);assert.equal(Buffer.byteLength(raw),bytes);
    const result=vm.runInNewContext(code,{installation:{installPath:'C:/owned'},backendRoot:'C:/owned/backend',launcher:'C:/owned/launcher.py',path,
      fs:{statSync:()=>({size:bytes}),existsSync:()=>true,readFileSync:()=>{reads++;return raw;}}});
    assert.equal(result,bytes<=(16<<20)?'a'.repeat(64):null);
    assert.equal(reads,bytes<=(16<<20)?1:0);
  }
});

function fixture(t,{multipart=false,unicode=false}={}) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'oa-desktop-backend-'));
  const state={events:[],submissions:0,resolutions:0,reconciliations:0,links:0,children:[],helperChildren:[],saved:null,
    server:{principalKind:1,principal:'fixture-sid',program:'fixture-runtime'},window:{logicalOwner:'durable-owner',historyEpoch:'epoch-1'},
    receipt:null,receipts:new Map(),observation:'complete',lostReply:false,unknown:false,listing:null,extractExtra:false,extractBad:false,afterChild:null};
  const key='oaDesktopBackendFixture'+(++sequence); globalThis[key]=state;
  const hooks=[];
  t.after(()=>{
    assert.ok([...state.children,...state.helperChildren].every(child=>child.closed),'every synthetic owned child reached terminal');
    for(const hook of hooks)hook.deregister();
    for(const filename of Object.keys(require.cache))if(filename.startsWith(root+path.sep))delete require.cache[filename];
    delete globalThis[key];
    const resolved=path.resolve(root),base=path.resolve(os.tmpdir());
    assert.ok(resolved.startsWith(base+path.sep) && path.basename(resolved).startsWith('oa-desktop-backend-'));
    fs.rmSync(resolved,{recursive:true,force:true});
    assert.equal(fs.existsSync(resolved),false);
    assert.deepEqual(physical,[]);
  });
  const resourceRoot=path.join(root,'resources'),sdkRoot=path.join(root,'sdk'),cacheRoot=path.join(root,'cache'),target=path.join(root,'new-instance');
  const nativeWindows=path.join(root,'native-windows');
  for (const p of [resourceRoot,sdkRoot,cacheRoot]) fs.mkdirSync(p);
  const put=(base,name,raw)=>{const filename=path.join(base,...name.split('/'));fs.mkdirSync(path.dirname(filename),{recursive:true});fs.writeFileSync(filename,raw);return pinned(name,raw);};
  const json=(base,name,value)=>put(base,name,JSON.stringify(value));
  const runtime={
    'standalone-env/python.exe':Buffer.from('fixture python'), 'standalone-env/uv.exe':Buffer.from('fixture uv'),
    'ComfyUI/oa_comfyui_launch.py':Buffer.from('fixture guarded launcher'), 'ComfyUI/oa_interpreter.py':Buffer.from('fixture interpreter'),
    'requirements-amd-win.txt':Buffer.from('fixture requirements'), 'OA-LICENSES/license.txt':Buffer.from('fixture licence'),
    'ComfyUI/empty.txt':Buffer.alloc(0)};
  if(unicode)runtime['standalone-env/models/FONLY__\uff0abf16.aks2']=Buffer.from('fixture Unicode DATA');
  const archiveRaw=Buffer.from('fixture logical archive bytes'),sourceRaw=Buffer.from('fixture source archive');
  const set=(name,raw,format)=>({...pinned(name,raw),format,parts:[pinned(name,raw)]});
  const archive=set('backend.7z',archiveRaw,'7z'),source=set('backend-source.zip',sourceRaw,'zip');
  const partBytes=multipart?{'backend.7z.001':archiveRaw.subarray(0,10),'backend.7z.002':archiveRaw.subarray(10)}:{'backend.7z':archiveRaw};
  archive.parts=Object.entries(partBytes).map(([name,raw])=>pinned(name,raw));
  const inputPin=json(resourceRoot,'backend-inputs.json',{schema:1,selected:'fixture-only'});
  const runtime_files=Object.fromEntries(Object.entries(runtime).map(([name,raw])=>[name,{bytes:raw.length,sha256:digest(raw)}]));
  const manifest={id:'win-amd',manager:false,input_descriptor_sha256:inputPin.sha256,comfyui_ref:'a'.repeat(40),comfyui_commit:'a'.repeat(40),
    python_version:'3.12.10',private_python_seal:{records_sha256:'b'.repeat(64)},runtime_files,
    oa_launcher:{...pinned('ComfyUI/oa_comfyui_launch.py',runtime['ComfyUI/oa_comfyui_launch.py']),
      interpreter:pinned('ComfyUI/oa_interpreter.py',runtime['ComfyUI/oa_interpreter.py'])},oa_integration:pinned('integration.zip',Buffer.from('fixture integration'))};
  const manifestPin=json(resourceRoot,'backend-manifest.json',manifest),manifestRaw=fs.readFileSync(path.join(resourceRoot,manifestPin.name));
  const toolRoot=path.join(resourceRoot,'archive-tool');fs.mkdirSync(toolRoot);
  const helper=put(resourceRoot,'launch-helper.exe','fixture OA signed helper');
  json(resourceRoot,'launch-helper-pin.json',helper);
  const tool=put(toolRoot,'7zr.exe','fixture tool'),toolSource=put(toolRoot,'7z2605-src.tar.xz','fixture tool source');
  const notices=['DOC/License.txt','DOC/copying.txt','DOC/unRarLicense.txt'].map(name=>{
    const p=put(toolRoot,'notices/'+name,'fixture '+name);return {...p,name};
  });
  const data_release={repository:'openabstractions/fixture-data',tag:'fixture-1'};
  const policy={comfyui_commit:manifest.comfyui_ref,python_version:manifest.python_version,data_release,
    limits:{asset_bytes:2147483648,expanded_bytes:17179869184,archive_bytes:8589934592,part_count:16,reserve_bytes:21474836480},
    archive_tool:{...tool,version:'26.05',source:{...toolSource,commit:'c'.repeat(40)},notices}};
  const policyPin=json(resourceRoot,'backend-amd-v1.json',policy);
  const producerPin=json(resourceRoot,'backend-producer.json',{schema:3,input_descriptor:{...inputPin,name:'original-inputs.json'},
    candidate_source:'d'.repeat(40),run_id:123,run_attempt:1,archive,source_pair:source});
  const resource={schema:2,variant:'win-amd',comfyui_ref:manifest.comfyui_ref,python_version:manifest.python_version,archive,
    corresponding_source:source,data_release,provenance:{producer_receipt:producerPin,input_descriptor:inputPin,manifest:manifestPin,backend_policy:policyPin}};
  json(resourceRoot,'desktop-backend.json',resource);
  const prefix=`const s=globalThis[${JSON.stringify(key)}];`;
  const modules={
    'node_modules/@openabstractions/ipc/package.json':JSON.stringify({type:'module'}),
    'node_modules/@openabstractions/ipc/native/fixture.node':'never loaded fixture addon',
    'node_modules/@openabstractions/ipc/fixture.cjs':`module.exports={marker:'current CommonJS'};`,
    'node_modules/@openabstractions/ipc/index.js':prefix+`
      import {createRequire} from 'node:module';const require=createRequire(import.meta.url);
      export function addonPath(){return s.addonOverride??'./native/fixture.node';}
      export class NativeConnector {async selectRuntime(){s.events.push('selectRuntime');return s.server;}connect(){s.cjsMarker=require('./fixture.cjs').marker;s.events.push('fixture-connect');return {};}}
    `,
    'node_modules/@openabstractions/facade/package.json':JSON.stringify({type:'module'}),
    'node_modules/@openabstractions/facade/transitive.js':`export const marker='current ESM';`,
    'node_modules/@openabstractions/facade/index.js':prefix+`
      import {marker} from './transitive.js';s.transitiveMarker=marker;
      s.lateImport=specifier=>import(specifier);
      export function validateReference(request,result){const r=result.reference;if(result.status!=='resolved'||r.contract!==request.contracts[0]||r.scope!=='local'||!request.guarantees.every(x=>r.guarantees.includes(x)))throw Error('invalid fixture reference');return r;}
      export class Binding {constructor(connector,endpoint,waiting,reference){this.connector=connector;this.endpoint=endpoint;this.waiting=waiting;this.reference=reference;}client(Client){return new Client(this);}}
      export class Machine {constructor(endpoint,options){this.connector=options.connector;}async resolveService(contract,options){s.resolutions++;const reference={provider:'fixture-provider',capability:'abstraction.job',contract,guarantees:options.guarantees,scope:'local',transport:'local',endpoint:'fixture-endpoint'};return new Binding(this.connector,reference.endpoint,{timeout:1000,maxFrame:1048576,server:s.server},reference);}}
    `,
    'node_modules/@openabstractions/job-acceptance/js/abstraction/job/acceptance/index.mjs':prefix+`
      export const admissionGuarantees=['abstraction.job/caller-exit@1','abstraction.job/service-restart@1','abstraction.job/reconciliation@1'];
      export const newSubmission=()=>({});
      const accepted=identity=>({outcome:s.unknown?'unknown':'accepted',receipt:s.receipts.get(identity.key)});
      export class RecoverableAcceptanceClient {
        constructor(binding){this.binding=binding;}
        async getHistoryWindow(){this.binding.connector.connect();s.events.push('window');return s.window;}
        async submit(value){this.binding.connector.connect();s.events.push('submit');s.submissions++;if(!s.saved)throw Error('not journalled');if(value.identity.attempt!==0n)throw Error('not BigInt');
          const spec=s.decodeSpec?s.decodeSpec(value.spec):JSON.parse(Buffer.from(value.spec)),name=spec.sources[0].locator.split('/').at(-1);if(String(spec.artifact.size)!==String(s.partBytes[name].length)||spec.sources[0].scheme!=='https'||spec.sources[0].credential!=='')throw Error('wrong encoded request');
          if(!s.receipts.has(value.identity.key))s.receipts.set(value.identity.key,{identity:value.identity,logicalOwner:s.window.logicalOwner,operationId:'operation-'+s.receipts.size,acceptedGuarantees:admissionGuarantees});
          s.receipt=s.receipts.get(value.identity.key);
          if(s.lostReply){s.lostReply=false;throw Error('lost reply');}return accepted(value.identity);}
        async reconcile(identity){this.binding.connector.connect();s.events.push('reconcile');s.reconciliations++;return accepted(identity);}
        async cancelWork(){throw Error('wait cancellation must not cancel work');}
      }
      export class OperationControlClient {
        constructor(binding){this.binding=binding;}
        async observeWork(identity){this.binding.connector.connect();s.events.push('observe');s.onObserve?.();return {outcome:'observed',snapshot:{receipt:s.receipts.get(identity.key),state:s.observation,progress:{done:BigInt(s.archiveRaw.length)}}};}
        async linkResult(identity,destination){this.binding.connector.connect();s.links++;s.events.push('link');fsWrite(destination);return {outcome:'linked'};}
      }
      function fsWrite(destination){s.link(destination);}
    `,
    'node_modules/@openabstractions/download-request/js/abstraction/download/request/index.mjs':`
      export const newRequest=()=>({});export const encode=value=>{if(typeof value.artifact.size!=='bigint')throw Error('size must be BigInt');return new TextEncoder().encode(JSON.stringify(value,(_,v)=>typeof v==='bigint'?String(v):v));};
    `};
  const files={};for(const [name,raw] of Object.entries(modules)){const p=put(sdkRoot,name,raw);files[name]={bytes:p.bytes,sha256:p.sha256};}
  const sdkPin=json(sdkRoot,'sdk.json',{schema:1,entry:'node_modules/@openabstractions/ipc/index.js',files});
  json(sdkRoot,'sdk-pin.json',sdkPin);
  state.archiveRaw=archiveRaw;state.partBytes=partBytes;state.link=destination=>fs.writeFileSync(destination,partBytes[path.basename(destination)]);
  const entries={...runtime,'manifest.json':manifestRaw};
  const listing=utf8=>{
    const text='7-Zip fixture\n----------\n'+Object.entries(entries).map(([name,raw])=>`Path = ${name}\nSize = ${raw.length}\nFolder = -\nEncrypted = -`).join('\n\n')+'\n';
    return Buffer.from(utf8?text:text.replaceAll('\uff0a','*'));
  };
  const spawn=(executable,args,options)=>{
    const helperCommand=['archive-environment','seal-backend'].includes(args[0]);
    const children=helperCommand?state.helperChildren:state.children;
    children.push({executable,args,options,closed:false});const event=children.at(-1),child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();
    child.kill=()=>{event.killed=true;return true;};
    queueMicrotask(()=>{
      let code=0;
      try {
        if(args[0]==='archive-environment') {
          const temporary=args[args.indexOf('--temporary')+1];
          const windows=nativeWindows;
          child.stdout.write(state.helperOutput??JSON.stringify({SystemRoot:windows,windir:windows,
            SystemDrive:path.parse(windows).root.slice(0,2),PATH:path.join(windows,'System32'),TEMP:temporary,TMP:temporary}));
        }
        else if(args[0]==='seal-backend')state.events.push('seal-backend');
        else if(args[0]==='l')child.stdout.write(state.listing??listing(args.includes('-sccUTF-8')));
        else if(args.includes('-so'))child.stdout.write(manifestRaw);
        else {const output=args.find(x=>x.startsWith('-o')).slice(2);for(const [name,raw] of Object.entries(entries))put(output,name,raw);
          if(state.extractExtra)put(output,'foreign.txt','foreign');if(state.extractBad)put(output,'standalone-env/python.exe','changed');}
        if(helperCommand)state.afterHelper?.(event);else state.afterChild?.(event);
      }catch(error){child.stderr.write(error.message);code=1;}
      child.stdout.end();child.stderr.end();event.closed=true;child.emit('close',code);
    });return child;
  };
  const bridgeFile=path.join(__dirname,'desktop_backend.cjs'),m=new Module(bridgeFile,module);m.filename=bridgeFile;m.paths=module.paths;
  const realRequire=m.require.bind(m),fakeFS=Object.create(fs);fakeFS.statfsSync=()=>({bavail:1000000000,bsize:4096});
  const moduleOwner=Object.create(Module);moduleOwner.registerHooks=value=>{const hook=Module.registerHooks(value);hooks.push(hook);return hook;};
  m.require=name=>name==='node:child_process'?{spawn}:name==='node:fs'?fakeFS:name==='node:module'?(state.unsupportedHooks?{}:moduleOwner):realRequire(name);
  m._compile(fs.readFileSync(bridgeFile,'utf8'),bridgeFile);
  const saveWork=async value=>{state.events.push('save');state.saved=clone(value);state.onSave?.(value);};
  const options={resource,resourceRoot,sdkRoot,sdkPin,target,cacheRoot,saveWork};
  return {root,state,bridge:m.exports,options,runtime,resource,manifest,policy,put,json,entries,hooks,modules,moduleOwner,nativeWindows,
    refresh:(role,value)=>{resource.provenance[role]=json(resourceRoot,resource.provenance[role].name,value);json(resourceRoot,'desktop-backend.json',resource);}};
}

test('closed resource refuses missing source release and unknown fields',t=>{
  t.diagnostic('test host '+process.version+'; Electron/native execution remains pending');
  const f=fixture(t);assert.deepEqual(f.bridge.readResource(f.options.resourceRoot),f.resource);
  const missing=clone(f.resource);delete missing.data_release;assert.throws(()=>f.bridge.validateResource(missing),/resource2/);
  const latest=clone(f.resource);latest.data_release.tag='latest';assert.throws(()=>f.bridge.validateResource(latest),/source-selected/);
  const extra=clone(f.resource);extra.url='https://ambient.invalid';assert.throws(()=>f.bridge.validateResource(extra),/resource2/);
  const wrong=clone(f.resource);wrong.archive.parts[0].name='../escape';assert.throws(()=>f.bridge.validateResource(wrong),/unsafe|archive/);
  const wildcard=clone(f.resource);wildcard.provenance.manifest.name='bad?.json';assert.throws(()=>f.bridge.validateResource(wildcard),/unsafe/);
});

test('current generated Download codec preserves exact BigInt source request',async()=>{
  const filename=path.resolve(__dirname,'../../openabstractions-flat/abstraction-download/javascript/js/abstraction/download/request/index.mjs');
  const codec=await import(pathToFileURL(filename).href);
  const request={...codec.newRequest(),artifact:{digest:'sha256:'+'a'.repeat(64),size:1234567890123n},
    sources:[{scheme:'https',locator:'https://github.com/openabstractions/fixture-data/releases/download/fixture-1/backend.7z',credential:''}]};
  assert.deepEqual(codec.decode(codec.encode(request)),request);assert.deepEqual(physical,[]);
});

test('exact pinned SDK and provenance refuse before acquisition or archive child',async t=>{
  const f=fixture(t);
  await assert.rejects(f.bridge.installBackend({...f.options,sdkPin:undefined}),/file pin/);
  await assert.rejects(f.bridge.installBackend({...f.options,sdkPin:{...f.options.sdkPin,sha256:'0'.repeat(64)}}),/metadata differs/);
  fs.appendFileSync(path.join(f.options.sdkRoot,'node_modules/@openabstractions/ipc/index.js'),'changed');
  await assert.rejects(f.bridge.installBackend(f.options),/admitted bytes|admitted size/);
  assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('ambient native connector and mutated provenance refuse without fallback',async t=>{
  const f=fixture(t);f.state.addonOverride=path.join(f.root,'outside.node');
  await assert.rejects(f.bridge.installBackend(f.options),/unsafe|outside/);
  f.state.addonOverride=null;fs.appendFileSync(path.join(f.options.resourceRoot,'backend-inputs.json'),' ');
  await assert.rejects(f.bridge.installBackend(f.options),/metadata differs/);
  assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('producer identity and complete SDK roster are required before acquisition',async t=>{
  const f=fixture(t),producer=JSON.parse(fs.readFileSync(path.join(f.options.resourceRoot,'backend-producer.json')));
  producer.run_id='unmeasured';f.refresh('producer_receipt',producer);
  await assert.rejects(f.bridge.installBackend(f.options),/producer binding/);
  producer.run_id=123;f.refresh('producer_receipt',producer);
  f.put(f.options.sdkRoot,'unadmitted.js','foreign');
  await assert.rejects(f.bridge.installBackend(f.options),/SDK file closure/);
  assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('addon selection cannot change in the asynchronous journal callback',async t=>{
  const f=fixture(t);f.state.onSave=()=>{f.state.addonOverride=path.join(f.root,'ambient.node');};
  await assert.rejects(f.bridge.installBackend(f.options),/native connector selection changed/);
  assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('journal precedes submit, shared job links local bytes and exact archive closure prepares without Python',async t=>{
  const f=fixture(t),progress=[];const result=await f.bridge.installBackend({...f.options,onProgress:p=>progress.push(p)});
  assert.equal(f.state.submissions,1);assert.equal(f.state.links,1);assert.equal(f.state.resolutions,2);
  assert.ok(f.state.events.indexOf('save')<f.state.events.indexOf('submit'));
  assert.equal(result.python,path.join(f.options.target,'standalone-env/python.exe'));
  assert.equal(progress[0].percent,100);assert.match(progress[0].status,/complete/);
  assert.equal(f.state.children.length,3);assert.ok(f.state.children.every(c=>c.closed));
  for(const child of f.state.children){assert.equal(child.options.shell,false);assert.equal(child.options.windowsHide,true);assert.equal(child.options.env.PATH,path.join(f.nativeWindows,'System32'));assert.equal(child.executable,path.join(f.options.resourceRoot,'archive-tool','7zr.exe'));}
  assert.equal(f.state.helperChildren.length,5);
  for(const child of f.state.helperChildren){assert.deepEqual(child.options.env,{});assert.equal(child.executable,path.join(f.options.resourceRoot,'launch-helper.exe'));}
  assert.deepEqual(result.correspondingSource,{archive:f.resource.corresponding_source,data_release:f.resource.data_release});
  assert.ok(!f.state.children.some(c=>/python|node/i.test(path.basename(c.executable))));
});

test('native environment carrier ignores hostile ambient roots before every archive spawn',async t=>{
  const f=fixture(t),keys=['SystemRoot','WINDIR','PATH','COMSPEC','PYTHONPATH','LD_PRELOAD','DYLD_INSERT_LIBRARIES'];
  const original=Object.fromEntries(keys.map(key=>[key,process.env[key]]));
  try {
    for(const key of keys)process.env[key]='Z:\\foreign-'+key;
    await f.bridge.installBackend(f.options);
    for(const child of f.state.helperChildren)assert.deepEqual(child.options.env,{});
    for(const child of f.state.children){
      assert.equal(child.options.env.SystemRoot,f.nativeWindows);
      assert.equal(child.options.env.PATH,path.join(f.nativeWindows,'System32'));
      for(const key of ['COMSPEC','PYTHONPATH','LD_PRELOAD','DYLD_INSERT_LIBRARIES'])assert.ok(!Object.hasOwn(child.options.env,key));
    }
  } finally {for(const key of keys)if(original[key]===undefined)delete process.env[key];else process.env[key]=original[key];}
});

test('malformed native environment and changed helper carrier refuse before archive effects',async t=>{
  for(const variant of ['malformed','mutation']){
    const f=fixture(t);
    if(variant==='malformed')f.state.helperOutput='{"SystemRoot":"Z:\\\\foreign"}';
    else f.state.afterHelper=()=>fs.appendFileSync(path.join(f.options.resourceRoot,'launch-helper.exe'),'changed');
    await assert.rejects(f.bridge.installBackend(f.options),/native Windows environment|admitted/);
    assert.equal(f.state.children.length,0);assert.equal(f.state.submissions,0);
    assert.equal(f.state.helperChildren.length,1);assert.ok(f.state.helperChildren[0].closed);
  }
});

test('lost submit reply reconciles original identity with no replacement provider',async t=>{
  const f=fixture(t);f.state.lostReply=true;await f.bridge.installBackend(f.options);
  assert.equal(f.state.submissions,1);assert.equal(f.state.reconciliations,1);assert.equal(f.state.resolutions,2);
});

test('ordered multipart bytes preserve logical archive identity and first-volume child input',async t=>{
  const f=fixture(t,{multipart:true});await f.bridge.installBackend(f.options);
  assert.equal(f.state.submissions,2);assert.equal(f.state.links,2);
  assert.ok(f.state.children.every(child=>child.args.some(arg=>arg===path.join(f.options.cacheRoot,'backend.7z.001'))));
  assert.equal(Object.keys(f.state.saved.parts).length,2);
  const wrong=clone(f.resource);wrong.archive.parts.reverse();assert.throws(()=>f.bridge.validateResource(wrong),/ordered archive part/);
});

test('window close after accepted receipt resumes saved operation without submit or duplicate bytes',async t=>{
  const f=fixture(t);f.state.onSave=value=>{if(value.parts['backend.7z'].receipt)throw Error('window closed');};
  await assert.rejects(f.bridge.installBackend(f.options),/window closed/);
  const saved=clone(f.state.saved);f.state.onSave=null;
  await f.bridge.installBackend({...f.options,work:saved});
  assert.equal(f.state.submissions,1);assert.equal(f.state.resolutions,2);assert.equal(f.state.reconciliations,1);assert.equal(f.state.links,1);
  assert.equal(f.state.receipt.identity.key,saved.parts['backend.7z'].identity.key);
});

test('unknown reconciliation keeps saved identity and refuses provider replacement',async t=>{
  const f=fixture(t);f.state.onSave=value=>{if(value.parts['backend.7z'].receipt)throw Error('window closed');};
  await assert.rejects(f.bridge.installBackend(f.options),/window closed/);f.state.onSave=null;f.state.unknown=true;
  await assert.rejects(f.bridge.installBackend({...f.options,work:f.state.saved}),/acceptance unknown/);
  assert.equal(f.state.submissions,1);assert.equal(f.state.resolutions,2);assert.equal(f.state.children.length,0);
});

test('saved source spec, epoch and server mutations refuse before another submit',async t=>{
  const f=fixture(t);f.state.onSave=()=>{throw Error('window closed');};await assert.rejects(f.bridge.installBackend(f.options),/window closed/);f.state.onSave=null;
  const saved=clone(f.state.saved),bad=clone(saved);bad.parts['backend.7z'].spec='changed';
  await assert.rejects(f.bridge.installBackend({...f.options,work:bad}),/saved request/);
  f.state.window={...f.state.window,historyEpoch:'epoch-2'};
  await assert.rejects(f.bridge.installBackend({...f.options,work:saved}),/history changed/);
  f.state.window.historyEpoch='epoch-1';f.state.server={...f.state.server,program:'foreign-runtime'};
  await assert.rejects(f.bridge.installBackend({...f.options,work:saved}),/binding changed/);
  assert.equal(f.state.submissions,0);assert.equal(f.state.resolutions,2);
});

test('wait cancellation retains accepted work and does not cancel shared job',async t=>{
  const f=fixture(t),controller=new AbortController();f.state.observation='running';f.state.onObserve=()=>controller.abort(Error('window wait stopped'));
  await assert.rejects(f.bridge.installBackend({...f.options,signal:controller.signal}),/window wait stopped/);
  f.state.onObserve=null;f.state.observation='complete';await f.bridge.installBackend({...f.options,work:f.state.saved});
  assert.equal(f.state.submissions,1);assert.equal(f.state.reconciliations,1);assert.equal(f.state.links,1);
});

test('cancellation arriving with complete observation does not start result linking',async t=>{
  const f=fixture(t),controller=new AbortController();f.state.onObserve=()=>controller.abort(Error('closed after observe'));
  await assert.rejects(f.bridge.installBackend({...f.options,signal:controller.signal}),/closed after observe/);
  assert.equal(f.state.links,0);assert.equal(f.state.children.length,0);assert.ok(f.state.saved.parts['backend.7z'].receipt);
});

test('nonempty target and changed cached bytes refuse without overwrite or archive execution',async t=>{
  const f=fixture(t);fs.mkdirSync(f.options.target);fs.writeFileSync(path.join(f.options.target,'owned.txt'),'keep');
  await assert.rejects(f.bridge.installBackend(f.options),/nonempty/);assert.equal(fs.readFileSync(path.join(f.options.target,'owned.txt'),'utf8'),'keep');
  const next=path.join(f.root,'other-new-instance');f.state.link=destination=>fs.writeFileSync(destination,'wrong bytes');
  await assert.rejects(f.bridge.installBackend({...f.options,target:next}),/admitted/);
  assert.equal(f.state.children.length,0);
});

test('archive traversal, missing roster and links refuse before extraction',async t=>{
  const f=fixture(t);
  for(const listing of ['----------\nPath = ../escape\nSize = 1\nFolder = -\n',
    '----------\nPath = standalone-env/python.exe\nSize = 14\nFolder = -\n',
    '----------\nPath = standalone-env/python.exe\nSize = 14\nSymbolic Link = outside\n']){
    f.state.listing=Buffer.from(listing);await assert.rejects(f.bridge.installBackend({...f.options,work:f.state.saved}),/unsafe|roster|required|archive/);
  }
  assert.equal(f.state.children.length,3);assert.ok(f.state.children.every(c=>c.args[0]==='l'&&c.closed));
  assert.deepEqual(fs.readdirSync(f.options.target),[]);assert.equal(f.state.submissions,1);assert.equal(f.state.links,1);
});

test('UTF8 listing preserves fullwidth filename identity through the actual receiver',async t=>{
  const f=fixture(t,{unicode:true});
  await f.bridge.installBackend(f.options);
  const name='standalone-env/models/FONLY__\uff0abf16.aks2';
  assert.deepEqual(fs.readFileSync(path.join(f.options.target,...name.split('/'))),f.runtime[name]);
  assert.deepEqual(f.state.children[0].args,['l','-slt','-sccUTF-8',path.join(f.options.cacheRoot,'backend.7z')]);
});

test('best-fit filename substitutions still refuse instead of normalizing the roster',async t=>{
  const f=fixture(t,{unicode:true});
  f.state.listing=Buffer.from('header\n----------\n'+Object.entries(f.entries).map(([name,raw])=>`Path = ${name.replaceAll('\uff0a','*')}\nSize = ${raw.length}\nFolder = -`).join('\n\n')+'\n');
  await assert.rejects(f.bridge.installBackend(f.options),/unsafe relative member/);
  assert.equal(f.state.children.length,1);
  assert.deepEqual(fs.readdirSync(f.options.target),[]);
});

test('extracted extra file and changed native bytes fail complete readback',async t=>{
  const f=fixture(t);f.state.extractExtra=true;
  await assert.rejects(f.bridge.installBackend(f.options),/closure differs/);
  assert.ok(fs.existsSync(path.join(f.options.target,'foreign.txt')));
  await assert.rejects(f.bridge.installBackend({...f.options,work:f.state.saved}),/nonempty/);
  const next=path.join(f.root,'second-instance');f.state.extractExtra=false;f.state.extractBad=true;
  await assert.rejects(f.bridge.installBackend({...f.options,target:next,work:f.state.saved}),/admitted/);
  assert.equal(f.state.submissions,1);assert.equal(f.state.links,1);
});

test('tool mutation across bounded child refuses before next child',async t=>{
  const f=fixture(t);f.state.afterChild=()=>fs.appendFileSync(path.join(f.options.resourceRoot,'archive-tool','7zr.exe'),'changed');
  await assert.rejects(f.bridge.installBackend(f.options),/admitted/);assert.equal(f.state.children.length,1);assert.ok(f.state.children[0].closed);
});

test('stable URL and transitive stale ESM exports cannot donate executable code',async t=>{
  const f=fixture(t),facade=path.join(f.options.sdkRoot,'node_modules/@openabstractions/facade/index.js'),
    transitive=path.join(path.dirname(facade),'transitive.js');
  const current=fs.readFileSync(facade),currentDependency=fs.readFileSync(transitive);
  fs.writeFileSync(facade,current.toString().replace('s.resolutions++;','s.staleExports=true;s.resolutions++;'));
  fs.writeFileSync(transitive,"export const marker='stale ESM';");
  const stale=await import(pathToFileURL(facade).href);assert.ok(stale.Machine);assert.equal(f.state.transitiveMarker,'stale ESM');
  fs.writeFileSync(facade,current);fs.writeFileSync(transitive,currentDependency);
  await f.bridge.installBackend(f.options);
  assert.equal(f.state.transitiveMarker,'current ESM');assert.equal(f.state.staleExports,undefined);
  assert.equal(f.state.cjsMarker,'current CommonJS');assert.equal(f.hooks.length,1);
});

test('preexisting native and CommonJS cache objects refuse without eviction or native load',async t=>{
  const f=fixture(t);
  for(const name of ['node_modules/@openabstractions/ipc/native/fixture.node','node_modules/@openabstractions/ipc/fixture.cjs']){
    const filename=path.join(f.options.sdkRoot,...name.split('/')),foreign={filename,loaded:true,exports:{stale:true}};
    require.cache[filename]=foreign;
    await assert.rejects(f.bridge.installBackend(f.options),/foreign or uncertain SDK/);
    assert.equal(require.cache[filename],foreign);delete require.cache[filename]; // this test owns the seeded object
  }
  assert.equal(f.hooks.length,0);assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('a foreign native object inserted after admission refuses before Submit',async t=>{
  const f=fixture(t),filename=path.join(f.options.sdkRoot,'node_modules/@openabstractions/ipc/native/fixture.node');
  const foreign={filename,loaded:true,exports:{stale:true}};
  f.state.onSave=()=>{require.cache[filename]=foreign;};
  await assert.rejects(f.bridge.installBackend(f.options),/foreign or uncertain SDK/);
  assert.equal(require.cache[filename],foreign);assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('uncertain CommonJS object donated during another load hook is refused',async t=>{
  const f=fixture(t),filename=path.join(f.options.sdkRoot,'node_modules/@openabstractions/ipc/fixture.cjs');
  const foreign={filename,loaded:false,exports:{stale:true}};
  const earlier=Module.registerHooks({load(url,context,nextLoad){
    const result=nextLoad(url,context);if(url.startsWith(pathToFileURL(filename).href))require.cache[filename]=foreign;return result;
  }});f.hooks.push(earlier);
  await assert.rejects(f.bridge.installBackend(f.options),/SDK cache object changed during load/);
  assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('same pin reuses one admitted graph and owned CJS objects; different pin or root requires restart',async t=>{
  const f=fixture(t);await f.bridge.installBackend(f.options);
  const cjs=path.join(f.options.sdkRoot,'node_modules/@openabstractions/ipc/fixture.cjs'),owned=require.cache[cjs];
  assert.ok(owned?.loaded);await f.bridge.installBackend({...f.options,target:path.join(f.root,'second-instance'),work:f.state.saved});
  assert.equal(require.cache[cjs],owned);assert.equal(f.hooks.length,1);assert.equal(f.state.submissions,1);assert.equal(f.state.links,1);
  await assert.rejects(f.bridge.installBackend({...f.options,sdkPin:{...f.options.sdkPin,sha256:'0'.repeat(64)}}),/replacement requires Electron restart/);
  const other=path.join(f.root,'other-sdk');fs.mkdirSync(other);
  await assert.rejects(f.bridge.installBackend({...f.options,sdkRoot:other}),/replacement requires Electron restart/);
  assert.equal(f.hooks.length,1);
});

test('lazy SDK imports keep the hook, reject escape/unpinned/changed source and pass unrelated application modules',async t=>{
  const f=fixture(t);await f.bridge.installBackend(f.options);
  const outside=f.put(f.root,'application.mjs',"export const application='ordinary';");
  const url=pathToFileURL(path.join(f.root,outside.name)).href;
  await assert.rejects(f.state.lateImport(url),/outside its pinned file roster/);
  assert.equal((await import(url)).application,'ordinary');
  assert.equal(typeof (await f.state.lateImport('node:path')).join,'function');
  const unpinned=f.put(f.options.sdkRoot,'node_modules/@openabstractions/facade/unpinned.mjs',"export const foreign=true;");
  await assert.rejects(f.state.lateImport('./unpinned.mjs'),/outside its pinned file roster/);
  fs.writeFileSync(path.join(f.options.sdkRoot,'node_modules/@openabstractions/facade/transitive.js'),"export const marker='changed';");
  await assert.rejects(f.state.lateImport('./transitive.js'),/admitted/);
  assert.equal(f.hooks.length,1);assert.equal(f.state.submissions,1);
});

test('unsupported standard hooks refuse before SDK execution or acquisition',async t=>{
  const f=fixture(t);f.moduleOwner.registerHooks=undefined;
  await assert.rejects(f.bridge.installBackend(f.options),/synchronous SDK admission hooks unavailable/);
  assert.equal(f.state.transitiveMarker,undefined);assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});

test('current generated Download transitive modules execute inside the admitted graph',async t=>{
  const f=fixture(t),base='node_modules/@openabstractions/download-request/js/abstraction/download/request/';
  const source=path.resolve(__dirname,'../../openabstractions-flat/abstraction-download/javascript/js/abstraction/download/request');
  const descriptor=JSON.parse(fs.readFileSync(path.join(f.options.sdkRoot,'sdk.json')));
  for(const name of ['index.mjs','internal.mjs']){
    const pin=f.put(f.options.sdkRoot,base+name,fs.readFileSync(path.join(source,name)));
    descriptor.files[base+name]={bytes:pin.bytes,sha256:pin.sha256};
  }
  f.options.sdkPin=f.json(f.options.sdkRoot,'sdk.json',descriptor);f.json(f.options.sdkRoot,'sdk-pin.json',f.options.sdkPin);
  const codec=await import(pathToFileURL(path.join(source,'index.mjs')).href);f.state.decodeSpec=codec.decode;
  const result=await f.bridge.installBackend(f.options);
  assert.equal(result.work.parts['backend.7z'].receipt.operationId,'operation-0');assert.equal(f.state.submissions,1);assert.equal(f.hooks.length,1);
});


test('runtime metadata alone admits cohort shape and retains entry and other metadata bounds',()=>{
  const source=fs.readFileSync(path.join(__dirname,'desktop_backend.cjs'),'utf8');
  const actual=source+'\nmodule.exports._receive={jsonFile,runtimeFiles};';
  const m={exports:{}};
  vm.runInNewContext(actual,{require,module:m,exports:m.exports,process,Buffer,URL,console,setTimeout,clearTimeout});
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'oa-manifest-bound-'));
  try {
    const names={}; for(let i=0;i<61240;i++)names['standalone-env/Lib/site-packages/cohort/'+i+'/'+ 'x'.repeat(95)]={bytes:0,sha256:'0'.repeat(64)};
    for(const name of ['standalone-env/python.exe','standalone-env/uv.exe','ComfyUI/oa_comfyui_launch.py','ComfyUI/oa_interpreter.py','requirements-amd-win.txt','OA-LICENSES/LICENSE'])
      names[name]={bytes:0,sha256:'0'.repeat(64)};
    const raw=process.env.OA_COMFY_COHORT_MANIFEST_TEST ? fs.readFileSync(process.env.OA_COMFY_COHORT_MANIFEST_TEST) : Buffer.from(JSON.stringify({runtime_files:names}));assert.ok(raw.length>13*1024**2&&raw.length<16*1024**2);
    const filename=path.join(root,'manifest.json');fs.writeFileSync(filename,raw);
    const data=m.exports._receive.jsonFile(filename,pinned('manifest.json',raw),16*1024**2);
    assert.equal(m.exports._receive.runtimeFiles(data).size,Object.keys(data.runtime_files).length);
    assert.throws(()=>m.exports._receive.jsonFile(filename),/metadata exceeds bound/);
    fs.writeFileSync(filename,Buffer.alloc(16*1024**2+1,32));
    assert.throws(()=>m.exports._receive.jsonFile(filename,null,16*1024**2),/metadata exceeds bound/);
    for(let i=61240;i<65537;i++)names['extra/'+i]={bytes:0,sha256:'0'.repeat(64)};
    assert.throws(()=>m.exports._receive.runtimeFiles({runtime_files:names}),/runtime file roster/);
  } finally { fs.rmSync(root,{recursive:true,force:true}); }
});

test('private outer producer provenance refuses before any public Desktop acquisition',async t=>{
  const f=fixture(t),producer=JSON.parse(fs.readFileSync(process.env.OA_COMFY_PRIVATE_OUTER_TEST || path.join(f.options.resourceRoot,'backend-producer.json')));
  delete producer.candidate_source; delete producer.run_id; delete producer.run_attempt;
  producer.source_mode='private-diagnostic';
  producer.private_provenance={source_commit:'a'.repeat(40),native_source_commit:'b'.repeat(40),native_library_sha256:'c'.repeat(64)};
  f.refresh('producer_receipt',producer);
  await assert.rejects(f.bridge.installBackend(f.options),/backend producer receipt/);
  assert.equal(f.state.submissions,0);assert.equal(f.state.children.length,0);
});
