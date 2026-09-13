'use strict';

// Electron's installation owner calls this before any backend Python starts.
// Acquisition, durable work and local result linking stay behind the shared SDK.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {pathToFileURL,fileURLToPath} = require('node:url');
const modules = require('node:module');
const {spawn} = require('node:child_process');

const META_LIMIT = 4 * 1024 ** 2;
const MANIFEST_LIMIT = 16 * 1024 ** 2;
const LIMITS = Object.freeze({asset_bytes:2 * 1024 ** 3, expanded_bytes:16 * 1024 ** 3,
  archive_bytes:8 * 1024 ** 3, part_count:16, reserve_bytes:20 * 1024 ** 3});
const PROVENANCE = Object.freeze({producer_receipt:'backend-producer.json', input_descriptor:'backend-inputs.json',
  manifest:'backend-manifest.json', backend_policy:'backend-amd-v1.json'});
const SHA = /^[0-9a-f]{64}$/;
const COMMIT = /^[0-9a-f]{40}$/;
// This fixed Electron module admits one SDK graph for its process lifetime.
// Its one synchronous hook remains installed for lazy createRequire() calls.
let sdkAdmission=null;
const fail = message => { throw new Error(`OA backend: ${message}`); };
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const ordered = value => Array.isArray(value) ? value.map(ordered) : plain(value) ?
  Object.fromEntries(Object.keys(value).sort().map(k=>[k,ordered(value[k])])) : value;
const same = (a,b) => JSON.stringify(ordered(a)) === JSON.stringify(ordered(b));
function closed(value, keys, label) {
  if (!plain(value) || Object.keys(value).sort().join('\0') !== [...keys].sort().join('\0')) fail(`invalid ${label}`);
  return value;
}
function member(name) {
  if (typeof name !== 'string' || !name || /[\x00-\x1f\\:<>"|?*]/.test(name) || name.startsWith('/') ||
      name.split('/').some(p => !p || p === '.' || p === '..' || /[. ]$/.test(p) ||
        /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(p))) fail('unsafe relative member');
  return name;
}
function pin(value, nameOnly=false) {
  closed(value,['name','bytes','sha256'],'file pin'); member(value.name);
  if (nameOnly && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value.name)) fail('invalid archive name');
  if (!Number.isSafeInteger(value.bytes) || value.bytes <= 0 || !SHA.test(value.sha256)) fail('invalid file identity');
  return value;
}
function archive(value, format) {
  closed(value,['name','bytes','sha256','format','parts'],'archive set');
  pin({name:value.name,bytes:value.bytes,sha256:value.sha256},true);
  if (value.format !== format || !value.name.endsWith(`.${format}`) || value.bytes > LIMITS.archive_bytes ||
      !Array.isArray(value.parts) || !value.parts.length || value.parts.length > LIMITS.part_count) fail('invalid archive limits');
  let total=0;
  value.parts.forEach((part,i) => {
    pin(part,true);
    if (part.name !== (value.parts.length === 1 ? value.name : `${value.name}.${String(i+1).padStart(3,'0')}`) ||
        part.bytes >= LIMITS.asset_bytes) fail('invalid ordered archive part');
    total += part.bytes;
  });
  if (total !== value.bytes || value.parts.length === 1 &&
      !same(value.parts[0],{name:value.name,bytes:value.bytes,sha256:value.sha256})) fail('archive parts differ from logical identity');
  return value;
}
function dataRelease(value) {
  closed(value,['repository','tag'],'DATA location');
  if (!/^openabstractions\/[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value.repository) ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.tag) || /^(latest|main|master|default|head)$/i.test(value.tag)) fail('missing source-selected DATA release');
  return value;
}
function validateResource(value) {
  closed(value,['schema','variant','comfyui_ref','python_version','archive','corresponding_source','data_release','provenance'],'resource2');
  if (value.schema !== 2 || value.variant !== 'win-amd' || !COMMIT.test(value.comfyui_ref) || value.python_version !== '3.12.10') fail('unsupported backend resource');
  archive(value.archive,'7z'); archive(value.corresponding_source,'zip'); dataRelease(value.data_release);
  const names=[...value.archive.parts,...value.corresponding_source.parts].map(p=>p.name.toLowerCase());
  if (new Set(names).size !== names.length) fail('archive parts alias');
  closed(value.provenance,Object.keys(PROVENANCE),'provenance');
  for (const [role,name] of Object.entries(PROVENANCE)) {
    const p=pin(value.provenance[role],true);
    if (p.name !== name || p.bytes > (role === 'manifest' ? MANIFEST_LIMIT : META_LIMIT)) fail('invalid provenance member');
  }
  return value;
}
function canonical(p) { return process.platform === 'win32' ? p.toLowerCase() : p; }
function ordinary(p, directory=false, linked=false) {
  if (!path.isAbsolute(p) || p.startsWith('\\\\') || p.includes('\0')) fail('an absolute local path is required');
  const full=path.resolve(p), parsed=path.parse(full);
  let at=parsed.root;
  for (const part of full.slice(parsed.root.length).split(path.sep).filter(Boolean)) {
    at=path.join(at,part); const st=fs.lstatSync(at);
    if (st.isSymbolicLink() || canonical(fs.realpathSync.native(at)) !== canonical(at)) fail('linked or aliased path');
  }
  const st=fs.lstatSync(full);
  if (directory ? !st.isDirectory() : !st.isFile()) fail('ordinary file or directory required');
  if (!directory && !linked && st.nlink !== 1) fail('aliased input file');
  return full;
}
function under(root,name) { member(name); return path.join(root,...name.split('/')); }
function hashFile(filename, expected, aggregate=null, linked=false) {
  ordinary(filename,false,linked); const fd=fs.openSync(filename,'r');
  try {
    const before=fs.fstatSync(fd,{bigint:true}), digest=crypto.createHash('sha256'), buffer=Buffer.alloc(65536);
    let bytes=0,n;
    while ((n=fs.readSync(fd,buffer,0,buffer.length,null))) {
      bytes+=n; if (bytes > expected.bytes) fail('file exceeds admitted size');
      digest.update(buffer.subarray(0,n)); aggregate?.update(buffer.subarray(0,n));
    }
    const after=fs.fstatSync(fd,{bigint:true});
    if (bytes !== expected.bytes || digest.digest('hex') !== expected.sha256 ||
        ['dev','ino','size','mtimeNs','ctimeNs'].some(k=>before[k] !== after[k])) fail('file differs from admitted bytes');
    ordinary(filename,false,linked); return bytes;
  } finally { fs.closeSync(fd); }
}
function jsonFile(filename, expected=null, limit=META_LIMIT) {
  ordinary(filename); const size=fs.statSync(filename).size;
  if (!size || size > limit) fail('metadata exceeds bound');
  const raw=fs.readFileSync(filename); if (raw.length !== size || expected &&
      (raw.length !== expected.bytes || crypto.createHash('sha256').update(raw).digest('hex') !== expected.sha256)) fail('metadata differs');
  ordinary(filename);
  return JSON.parse(raw.toString('utf8'));
}
function readResource(resourceRoot) {
  ordinary(resourceRoot,true);
  return validateResource(jsonFile(path.join(resourceRoot,'desktop-backend.json')));
}
function runtimeFiles(manifest) {
  if (!plain(manifest.runtime_files) || !Object.keys(manifest.runtime_files).length || Object.keys(manifest.runtime_files).length > 65536) fail('missing complete runtime file roster');
  const result=new Map(), folded=new Set(); let total=0;
  for (const [name,p] of Object.entries(manifest.runtime_files)) {
    member(name); closed(p,['bytes','sha256'],'runtime file');
    if (!Number.isSafeInteger(p.bytes) || p.bytes < 0 || !SHA.test(p.sha256)) fail('invalid runtime file identity');
    if (name === 'manifest.json' || folded.has(name.toLowerCase())) fail('runtime member alias');
    folded.add(name.toLowerCase()); result.set(name,p); total+=p.bytes;
    if (total > LIMITS.expanded_bytes) fail('backend expanded budget exceeded');
  }
  for (const required of ['standalone-env/python.exe','standalone-env/uv.exe','ComfyUI/oa_comfyui_launch.py','ComfyUI/oa_interpreter.py','requirements-amd-win.txt'])
    if (!result.has(required)) fail(`missing runtime member ${required}`);
  if (![...result.keys()].some(n=>n.startsWith('OA-LICENSES/'))) fail('missing runtime licence inventory');
  return result;
}
function provenance(resource,root) {
  const data={}; for (const [role,p] of Object.entries(resource.provenance)) data[role]=jsonFile(under(root,p.name),p,role === 'manifest' ? MANIFEST_LIMIT : META_LIMIT);
  const policy=data.backend_policy, manifest=data.manifest, producer=data.producer_receipt;
  if (!same(policy.limits,LIMITS) || !same(dataRelease(policy.data_release),resource.data_release) ||
      policy.comfyui_commit !== resource.comfyui_ref || policy.python_version !== resource.python_version) fail('backend source policy differs');
  closed(producer,['schema','input_descriptor','candidate_source','run_id','run_attempt','archive','source_pair'],'backend producer receipt');
  pin(producer.input_descriptor,true);
  if (producer.schema !== 3 || !COMMIT.test(producer.candidate_source) ||
      !Number.isSafeInteger(producer.run_id) || producer.run_id <= 0 ||
      !Number.isSafeInteger(producer.run_attempt) || producer.run_attempt <= 0 ||
      !same(producer.archive,resource.archive) || !same(producer.source_pair,resource.corresponding_source) ||
      producer.input_descriptor?.sha256 !== resource.provenance.input_descriptor.sha256 ||
      producer.input_descriptor?.bytes !== resource.provenance.input_descriptor.bytes) fail('backend producer binding differs');
  if (manifest.id !== 'win-amd' || manifest.manager !== false || manifest.input_descriptor_sha256 !== resource.provenance.input_descriptor.sha256 ||
      manifest.comfyui_ref !== resource.comfyui_ref || manifest.comfyui_commit !== resource.comfyui_ref ||
      manifest.python_version !== resource.python_version || !SHA.test(manifest.private_python_seal?.records_sha256)) fail('backend manifest differs');
  const files=runtimeFiles(manifest);
  for (const [role,name] of [['oa_launcher','ComfyUI/oa_comfyui_launch.py']]) {
    const p=manifest[role];
    if (p?.name !== name || !same(files.get(name),{bytes:p.bytes,sha256:p.sha256})) fail('launcher identity differs');
    const interpreter=p.interpreter;
    if (interpreter?.name !== 'ComfyUI/oa_interpreter.py' ||
        !same(files.get(interpreter.name),{bytes:interpreter.bytes,sha256:interpreter.sha256})) fail('interpreter guard identity differs');
  }
  pin(manifest.oa_integration,true);
  return {...data,files};
}

function treeFiles(root) {
  const files=[];
  function visit(directory,prefix='') {
    for (const name of fs.readdirSync(directory)) {
      const relative=prefix+name; member(relative);
      const full=under(root,relative), st=fs.lstatSync(full);
      if (st.isSymbolicLink()) fail('linked tree member');
      if (st.isDirectory()) visit(full,relative+'/');
      else if (st.isFile()) files.push(relative);
      else fail('nonordinary tree member');
    }
  }
  ordinary(root,true); visit(root); return files.sort();
}
function sdkGraph(root,descriptor,descriptorPin) {
  if (sdkAdmission) {
    if (canonical(root) !== sdkAdmission.root || !same(descriptorPin,sdkAdmission.pin)) fail('SDK replacement requires Electron restart');
    sdkAdmission.checkCache(); return sdkAdmission;
  }
  if (typeof modules.registerHooks !== 'function') fail('synchronous SDK admission hooks unavailable; Electron restart or supported build required');
  const prefix=canonical(root+path.sep), owned=new Map(), loading=new Set(), resolvedLoads=new Set();
  const relative=filename=>{
    if (!path.isAbsolute(filename) || !canonical(filename).startsWith(prefix)) return null;
    const name=path.relative(root,filename).split(path.sep).join('/'); member(name); return name;
  };
  const selected=url=>{
    if (typeof url !== 'string' || !url.startsWith('file:')) return null;
    const filename=fileURLToPath(url),name=relative(filename);
    return name === null ? null : {filename,name,pin:descriptor.files[name]};
  };
  const cacheEntry=filename=>require.cache[filename];
  const checkCache=()=>{
    for (const [filename,entry] of Object.entries(require.cache)) {
      const name=relative(filename); if (name === null) continue;
      if (!Object.hasOwn(descriptor.files,name) || owned.get(filename) !== entry || !entry.loaded)
        fail('foreign or uncertain SDK CommonJS/native cache; Electron restart required');
      hashFile(filename,descriptor.files[name]);
    }
  };
  checkCache();
  const nonce=crypto.randomBytes(24).toString('hex'), graphTag=descriptorPin.sha256+'.'+nonce;
  const qualify=url=>{const value=new URL(url);value.search='oa_sdk='+graphTag;value.hash='';return value.href;};
  const checkResolved=(result,forLoad=false)=>{
    const value=selected(result.url);
    if (!value?.pin) fail('SDK module resolved outside its pinned file roster');
    ordinary(value.filename); hashFile(value.filename,value.pin);
    const cached=cacheEntry(value.filename);
    // Node creates a new CommonJS cache object between our synchronous resolve
    // and load hooks. Only that just-resolved, still-unloaded object is ours.
    if (cached && owned.get(value.filename) !== cached &&
        !(forLoad && resolvedLoads.has(value.filename) && !cached.loaded)) fail('foreign SDK CommonJS/native cache; Electron restart required');
    return value;
  };
  const hook=modules.registerHooks({
    resolve(specifier,context,nextResolve) {
      const result=nextResolve(specifier,context);
      if (modules.isBuiltin(result.url)) return result;
      const fromSDK=selected(context.parentURL) !== null, intoSDK=selected(result.url) !== null;
      if (!fromSDK && !intoSDK) return result;
      const value=checkResolved(result);
      if (!cacheEntry(value.filename)) resolvedLoads.add(value.filename);
      // Native addons keep Node's canonical CommonJS cache key. Every ESM and
      // source dependency gets this fresh, descriptor-bound admission URL.
      return value.name.endsWith('.node') ? result : {...result,url:qualify(result.url)};
    },
    load(url,context,nextLoad) {
      const value=selected(url); if (!value) return nextLoad(url,context);
      checkResolved({url},true);
      const beforeCache=cacheEntry(value.filename);
      const result=nextLoad(url,context);
      if (cacheEntry(value.filename) !== beforeCache) fail('SDK cache object changed during load; Electron restart required');
      if (!['module','commonjs','json','addon'].includes(result.format)) fail('unsupported SDK module format');
      let source=null;
      if (result.format !== 'addon') {
        // Return the authenticated snapshot itself, rather than source donated
        // by a previous loader or a read performed after verification.
        source=fs.readFileSync(value.filename);
        if (source.length !== value.pin.bytes || crypto.createHash('sha256').update(source).digest('hex') !== value.pin.sha256) fail('SDK module source changed');
        ordinary(value.filename);
      } else hashFile(value.filename,value.pin);
      if (['commonjs','json','addon'].includes(result.format)) {
        loading.add(value.filename);
        const entry=cacheEntry(value.filename); if (entry) owned.set(value.filename,entry);
      }
      resolvedLoads.delete(value.filename);
      return {...result,source,shortCircuit:true};
    }
  });
  const finishLoads=()=>{
    for (const filename of loading) {
      const entry=cacheEntry(filename);
      if (entry?.loaded) owned.set(filename,entry);
      else if (entry) fail('uncertain SDK cache initialization; Electron restart required');
    }
    loading.clear(); checkCache();
  };
  sdkAdmission={root:canonical(root),pin:structuredClone(descriptorPin),hook,qualify,checkCache,finishLoads,promise:null};
  return sdkAdmission;
}
async function loadSDK(root,descriptorPin) {
  // The caller carries this pin from the source-admitted Desktop build inputs
  // in its signed complete container. Measuring sdk.json is not admission.
  pin(descriptorPin,true);
  if (descriptorPin.name !== 'sdk.json' || descriptorPin.bytes > META_LIMIT) fail('missing admitted SDK descriptor pin');
  ordinary(root,true);
  if (sdkAdmission && (canonical(root) !== sdkAdmission.root || !same(descriptorPin,sdkAdmission.pin))) fail('SDK replacement requires Electron restart');
  const descriptor=jsonFile(path.join(root,'sdk.json'),descriptorPin);
  closed(descriptor,['schema','entry','files'],'SDK role');
  if (descriptor.schema !== 1 || !plain(descriptor.files) || !Object.keys(descriptor.files).length) fail('missing current SDK role');
  member(descriptor.entry);
  if (!Object.hasOwn(descriptor.files,descriptor.entry)) fail('SDK entry is not pinned');
  const names=Object.keys(descriptor.files), folded=new Set();
  for (const name of names) {
    member(name); const p=descriptor.files[name]; closed(p,['bytes','sha256'],'SDK file');
    if (!Number.isSafeInteger(p.bytes) || p.bytes < 0 || !SHA.test(p.sha256) || folded.has(name.toLowerCase())) fail('invalid SDK roster');
    folded.add(name.toLowerCase()); hashFile(under(root,name),p);
  }
  const actual=treeFiles(root).filter(n=>!['sdk.json','sdk-pin.json'].includes(n));
  if (!same(actual,names.sort())) fail('SDK file closure differs');
  const paths={identity:'node_modules/@openabstractions/ipc/index.js', facade:'node_modules/@openabstractions/facade/index.js',
    jobs:'node_modules/@openabstractions/job-acceptance/js/abstraction/job/acceptance/index.mjs',
    download:'node_modules/@openabstractions/download-request/js/abstraction/download/request/index.mjs'};
  for (const name of Object.values(paths)) if (!Object.hasOwn(descriptor.files,name)) fail(`SDK lacks ${name}`);
  const graph=sdkGraph(root,descriptor,descriptorPin);
  if (graph.promise) return graph.promise;
  graph.promise=(async()=>{
  const identity=await import(graph.qualify(pathToFileURL(under(root,paths.identity)).href));
  const selectedAddon=()=>{
    const selected=identity.addonPath();
    return path.isAbsolute(selected)?selected:path.resolve(path.dirname(under(root,paths.identity)),selected);
  };
  const addon=selectedAddon();
  const relative=path.relative(root,addon).split(path.sep).join('/'); member(relative);
  if (!Object.hasOwn(descriptor.files,relative) || !relative.endsWith('.node')) fail('native connector is outside the admitted SDK');
  hashFile(addon,descriptor.files[relative]);
  const [facade,jobs,download]=await Promise.all(['facade','jobs','download'].map(k=>import(graph.qualify(pathToFileURL(under(root,paths[k])).href))));
  for (const [name,p] of Object.entries(descriptor.files)) hashFile(under(root,name),p);
  jsonFile(path.join(root,'sdk.json'),descriptorPin);
  const nativeConnector=new identity.NativeConnector();
  graph.finishLoads();
  // The shared connector chooses its addon at its first native call. Preserve
  // that admitted selection across asynchronous journal callbacks too.
  const connector=new Proxy(nativeConnector,{get(target,key){
    const value=Reflect.get(target,key,target);
    return typeof value === 'function' ? (...args)=>{
      if (canonical(selectedAddon()) !== canonical(addon)) fail('native connector selection changed');
      graph.checkCache();
      try { return Reflect.apply(value,target,args); }
      finally { graph.finishLoads(); }
    } : value;
  }});
  return {connector,facade,jobs,download,descriptor};
  })();
  return graph.promise;
}
function abort(signal) { if (signal?.aborted) throw signal.reason ?? new Error('OA backend waiting cancelled; saved work retained'); }
async function pause(signal) {
  abort(signal);
  await new Promise((resolve,reject)=>{
    const stop=()=>{clearTimeout(timer);signal.removeEventListener('abort',stop);reject(signal.reason ?? new Error('waiting cancelled'));};
    const timer=setTimeout(()=>{signal?.removeEventListener('abort',stop);resolve();},250);
    signal?.addEventListener('abort',stop,{once:true}); if (signal?.aborted) stop();
  });
}
function savedBinding(binding,contract,sdk) {
  sdk.facade.validateReference({capability:'abstraction.job',contracts:[contract],guarantees:sdk.jobs.admissionGuarantees,scope:'local'},
    {status:'resolved',reference:binding.reference});
  const {timeout,maxFrame,server}=binding.waiting;
  if (!server || ![1,2].includes(server.principalKind) || !server.principal || !server.program) fail('job binding lacks independent server expectation');
  return {endpoint:binding.endpoint,reference:structuredClone(binding.reference),waiting:{timeout,maxFrame,server:structuredClone(server)}};
}
function restoreBinding(value,contract,sdk,signal) {
  closed(value,['endpoint','reference','waiting'],'saved binding');
  closed(value.waiting,['timeout','maxFrame','server'],'saved waiting');
  if (value.endpoint !== value.reference?.endpoint) fail('saved endpoint differs from reference');
  const binding=new sdk.facade.Binding(sdk.connector,value.endpoint,{...value.waiting,cancellation:signal},value.reference);
  savedBinding(binding,contract,sdk); return binding;
}
function identityOf(value) {
  closed(value,['key','historyEpoch','attempt'],'saved request identity');
  if (typeof value.key !== 'string' || !value.key || typeof value.historyEpoch !== 'string' || !value.historyEpoch || value.attempt !== '0') fail('invalid saved request identity');
  return {...value,attempt:0n};
}
function checkReceipt(receipt,partWork,guarantees) {
  if (!receipt || receipt.identity?.key !== partWork.identity.key || receipt.identity.historyEpoch !== partWork.identity.historyEpoch ||
      receipt.identity.attempt !== 0n || receipt.logicalOwner !== partWork.window.logicalOwner ||
      typeof receipt.operationId !== 'string' || !receipt.operationId ||
      !Array.isArray(receipt.acceptedGuarantees) || !guarantees.every(g=>receipt.acceptedGuarantees.includes(g))) fail('job receipt differs from saved work');
  if (partWork.receipt && (partWork.receipt.operationId !== receipt.operationId || partWork.receipt.logicalOwner !== receipt.logicalOwner)) fail('accepted operation changed');
  return {operationId:receipt.operationId,logicalOwner:receipt.logicalOwner};
}
async function acquirePart({resource,part,record,sdk,cacheRoot,cacheGuard,saveWork,signal,onProgress}) {
  const url=`https://github.com/${resource.data_release.repository}/releases/download/${resource.data_release.tag}/${part.name}`;
  const request={...sdk.download.newRequest(),artifact:{digest:'sha256:'+part.sha256,size:BigInt(part.bytes)},
    sources:[{scheme:'https',locator:url,credential:''}]};
  const spec=Buffer.from(sdk.download.encode(request)), spec64=spec.toString('base64'), specHash=crypto.createHash('sha256').update(spec).digest('hex');
  let work=record.parts[part.name], acceptance,operations;
  if (!work) {
    const machine=new sdk.facade.Machine(null,{connector:sdk.connector,cancellation:signal});
    const requirements={scope:'local',guarantees:sdk.jobs.admissionGuarantees};
    acceptance=await machine.resolveService('abstraction.job/acceptance@1',requirements);
    operations=await machine.resolveService('abstraction.job/operations@1',requirements);
    const a=savedBinding(acceptance,'abstraction.job/acceptance@1',sdk),o=savedBinding(operations,'abstraction.job/operations@1',sdk);
    if (a.reference.provider !== o.reference.provider || a.endpoint !== o.endpoint || !same(a.waiting.server,o.waiting.server)) fail('job services differ');
    const window=await acceptance.client(sdk.jobs.RecoverableAcceptanceClient).getHistoryWindow();
    if (!window.logicalOwner || !window.historyEpoch) fail('job history is unavailable');
    const key='comfy-backend-'+crypto.createHash('sha256').update(record.resource_sha256+'\0'+part.name).digest('hex').slice(0,32);
    work={identity:{key,historyEpoch:window.historyEpoch,attempt:'0'},window:{logicalOwner:window.logicalOwner,historyEpoch:window.historyEpoch},
      acceptance:a,operations:o,spec:spec64,spec_sha256:specHash,receipt:null};
    record.parts[part.name]=work;
    // The installation owner must durably finish this update before Submit.
    await saveWork(structuredClone(record));
  } else {
    closed(work,['identity','window','acceptance','operations','spec','spec_sha256','receipt'],'saved part work');
    closed(work.window,['logicalOwner','historyEpoch'],'saved history');
    if (work.receipt !== null) closed(work.receipt,['operationId','logicalOwner'],'accepted work receipt');
    identityOf(work.identity);
    if (work.spec !== spec64 || work.spec_sha256 !== specHash || work.window.historyEpoch !== work.identity.historyEpoch) fail('saved request differs from source-selected bytes');
    acceptance=restoreBinding(work.acceptance,'abstraction.job/acceptance@1',sdk,signal);
    operations=restoreBinding(work.operations,'abstraction.job/operations@1',sdk,signal);
    if (work.acceptance.reference.provider !== work.operations.reference.provider || acceptance.endpoint !== operations.endpoint ||
        !same(work.acceptance.waiting.server,work.operations.waiting.server) ||
        !same(await sdk.connector.selectRuntime({cancellation:signal}),work.acceptance.waiting.server)) fail('saved runtime binding changed');
    const window=await acceptance.client(sdk.jobs.RecoverableAcceptanceClient).getHistoryWindow();
    if (window.logicalOwner !== work.window.logicalOwner || window.historyEpoch !== work.window.historyEpoch) fail('saved job history changed');
  }
  abort(signal);
  const client=acceptance.client(sdk.jobs.RecoverableAcceptanceClient), identity=identityOf(work.identity);
  let accepted;
  if (work.receipt) {
    accepted=await client.reconcile(identity);
  } else {
    const submission={...sdk.jobs.newSubmission(),identity,kind:'download',spec,requiredGuarantees:[...sdk.jobs.admissionGuarantees],label:'ComfyUI AMD backend: '+part.name};
    try { accepted=await client.submit(submission); }
    catch (error) { abort(signal); accepted=await client.reconcile(identity); }
  }
  if (accepted.outcome !== 'accepted') fail(`download acceptance ${accepted.outcome}; same saved work retained`);
  const receipt=checkReceipt(accepted.receipt,work,sdk.jobs.admissionGuarantees);
  if (!work.receipt) { work.receipt=receipt; await saveWork(structuredClone(record)); }
  const control=operations.client(sdk.jobs.OperationControlClient);
  for (;;) {
    abort(signal);
    const observed=await control.observeWork(identity);
    abort(signal);
    if (observed.outcome !== 'observed' || !observed.snapshot) fail(`download observation ${observed.outcome}; saved work retained`);
    const snapshot=observed.snapshot; checkReceipt(snapshot.receipt,work,sdk.jobs.admissionGuarantees);
    const done=Number(snapshot.progress?.done ?? 0n);
    onProgress?.({part:part.name,state:snapshot.state,done,total:part.bytes,
      percent:Math.max(0,Math.min(100,done/part.bytes*100)),status:`${part.name}: ${snapshot.state}`});
    if (['failed','cancelled'].includes(snapshot.state)) fail(`download ${snapshot.state}; explicit retry disposition required`);
    if (snapshot.state === 'complete') break;
    await pause(signal);
  }
  const destination=under(cacheRoot,part.name);
  abort(signal); cacheGuard();
  if (!fs.existsSync(destination)) {
    const linked=await control.linkResult(identity,destination);
    if (linked.outcome !== 'linked') fail(`local result link ${linked.outcome}; no private-store fallback`);
  }
  hashFile(destination,part,null,true);
}

function toolInputs(root,policy) {
  const tool=policy.archive_tool;
  if (!plain(tool) || tool.version !== '26.05' || tool.name !== '7zr.exe' || !Array.isArray(tool.notices) || tool.notices.length !== 3) fail('missing source-selected archive tool');
  const directory=path.join(root,'archive-tool'); ordinary(directory,true);
  const check=()=>{
    const expected=[tool.name,tool.source?.name,...tool.notices.map(p=>'notices/'+p.name)].sort();
    if (!same(treeFiles(directory),expected)) fail('archive tool file closure differs');
    hashFile(path.join(directory,tool.name),pin({name:tool.name,bytes:tool.bytes,sha256:tool.sha256},true));
    const source=tool.source;
    if (!source || source.name !== '7z2605-src.tar.xz' || !COMMIT.test(source.commit)) fail('missing archive tool source');
    hashFile(path.join(directory,source.name),pin({name:source.name,bytes:source.bytes,sha256:source.sha256},true));
    const notices=tool.notices.map(p=>p.name).sort();
    if (!same(notices,['DOC/License.txt','DOC/copying.txt','DOC/unRarLicense.txt'].sort())) fail('archive tool licence set differs');
    for (const p of tool.notices) hashFile(under(path.join(directory,'notices'),pin(p).name),p);
  };
  check(); return {filename:path.join(directory,tool.name),directory,check};
}
function verifyParts(cacheRoot,value) {
  const digest=crypto.createHash('sha256'); let total=0;
  for (const p of value.parts) total+=hashFile(under(cacheRoot,p.name),p,digest,true);
  if (total !== value.bytes || digest.digest('hex') !== value.sha256) fail('logical archive differs from ordered parts');
}
function listMembers(raw,files,manifestPin) {
  const text=raw.toString('utf8').replace(/\r\n/g,'\n');
  const split=text.indexOf('----------'); if (split < 0) fail('archive listing unavailable');
  const wanted=new Map(files); wanted.set('manifest.json',{bytes:manifestPin.bytes,sha256:manifestPin.sha256});
  const directories=new Set(); for (const name of wanted.keys()) {
    const parts=name.split('/'); for (let i=1;i<parts.length;i++) directories.add(parts.slice(0,i).join('/'));
  }
  const seen=new Set(),folded=new Set(); let expanded=0;
  for (const block of text.slice(split+10).trim().split('\n\n').filter(Boolean)) {
    const fields={};
    for (const line of block.split('\n')) {
      const at=line.indexOf(' = '); if (at < 0) continue;
      const key=line.slice(0,at); if (Object.hasOwn(fields,key)) fail('duplicate archive listing field');
      fields[key]=line.slice(at+3);
    }
    const name=member((fields.Path ?? '').replace(/\\/g,'/'));
    if (folded.has(name.toLowerCase()) || fields.Encrypted === '+' || Object.hasOwn(fields,'Symbolic Link') || Object.hasOwn(fields,'Hard Link')) fail('unsafe archive member');
    folded.add(name.toLowerCase());
    if (!/^(0|[1-9][0-9]*)$/.test(fields.Size ?? '')) fail('archive member lacks bounded size');
    const size=Number(fields.Size); if (!Number.isSafeInteger(size)) fail('archive size exceeds exact integer range');
    const directory=fields.Folder === '+' || /^D/.test(fields.Attributes ?? '');
    if (directory) { if (!directories.has(name) || size !== 0) fail('foreign archive directory'); }
    else {
      if (!wanted.has(name) || wanted.get(name).bytes !== size) fail('archive member differs from complete runtime roster');
      seen.add(name); expanded+=size;
      if (expanded > LIMITS.expanded_bytes) fail('archive expanded budget exceeded');
    }
  }
  if (!same([...seen].sort(),[...wanted.keys()].sort())) fail('archive lacks required runtime files');
  return expanded;
}
function helperInputs(root) {
  const carrier=under(root,'launch-helper-pin.json');ordinary(carrier);
  if (fs.statSync(carrier).size>4096) fail('helper carrier exceeds bound');
  const raw=fs.readFileSync(carrier),expected=pin(JSON.parse(raw),true);
  if (expected.name!=='launch-helper.exe') fail('helper carrier selects a foreign native role');
  const filename=under(root,expected.name);
  const check=()=>{
    if (!fs.readFileSync(carrier).equals(raw)) fail('helper carrier changed');
    hashFile(filename,expected);
  };
  check();return {filename,directory:root,check};
}
async function boundedNativeChild(tool,args,{env,cwd,signal,onOutput,limit=16*1024**2,timeout=30000}) {
  abort(signal);tool.check();
  try { return await new Promise((resolve,reject)=>{
    const child=spawn(tool.filename,args,{cwd:tool.directory,env,shell:false,windowsHide:true,stdio:['ignore','pipe','pipe']});
    const chunks=[]; let stdout=0,stderr=0,problem=null;
    const stop=error=>{problem??=error;child.kill();};
    const cancel=()=>stop(signal.reason ?? new Error('archive wait cancelled'));
    const timer=setTimeout(()=>stop(new Error('bounded archive child timed out')),timeout);
    signal?.addEventListener('abort',cancel,{once:true});
    child.stdout.on('data',data=>{stdout+=data.length;if(stdout>limit)stop(new Error('archive output exceeds bound'));else chunks.push(data);});
    child.stderr.on('data',data=>{stderr+=data.length;if(stderr>1024**2)stop(new Error('archive diagnostics exceed bound'));else {try {onOutput?.(data.toString('utf8'));} catch(error){stop(error);}}});
    child.on('error',error=>{problem??=error;});
    child.on('close',code=>{
      clearTimeout(timer);signal?.removeEventListener('abort',cancel);
      if (problem || code !== 0) reject(problem ?? new Error(`archive child exited ${code}`));
      else resolve(Buffer.concat(chunks));
    });
    if (signal?.aborted) cancel();
  }); } finally { tool.check(); }
}
async function archiveEnvironment(helper,temporary,backendParent,options) {
  const raw=await boundedNativeChild(helper,['archive-environment','--temporary',temporary,'--backend-parent',backendParent],
    {...options,cwd:helper.directory,env:{},limit:4096});
  const value=closed(JSON.parse(raw),['SystemRoot','windir','SystemDrive','PATH','TEMP','TMP'],'native Windows environment');
  if (Object.values(value).some(v=>typeof v!=='string'||v.includes('\0')) ||
      !path.win32.isAbsolute(value.SystemRoot) || value.windir!==value.SystemRoot ||
      value.SystemDrive!==path.win32.parse(value.SystemRoot).root.slice(0,2) ||
      value.PATH!==path.win32.join(value.SystemRoot,'System32') || canonical(value.TEMP)!==canonical(temporary) || canonical(value.TMP)!==canonical(temporary)) {
    fail('native Windows environment differs from fixed build policy');
  }
  return value;
}
async function archiveChild(tool,args,{helper,backendParent,...options}) {
  const env=await archiveEnvironment(helper,options.cwd,backendParent,options);
  return boundedNativeChild(tool,args,{...options,env});
}
function ensureCapacity(target,cacheRoot,archiveBytes,expandedBytes) {
  const targetInfo=fs.statfsSync(target), cacheInfo=fs.statfsSync(cacheRoot);
  const targetFree=BigInt(targetInfo.bavail)*BigInt(targetInfo.bsize), cacheFree=BigInt(cacheInfo.bavail)*BigInt(cacheInfo.bsize);
  const sameVolume=fs.statSync(target).dev === fs.statSync(cacheRoot).dev;
  if (targetFree < BigInt(expandedBytes+LIMITS.reserve_bytes+(sameVolume?archiveBytes:0)) ||
      cacheFree < BigInt(archiveBytes+LIMITS.reserve_bytes)) fail('backend capacity is below source-owned reserve');
}
async function installBackend({resource,resourceRoot,sdkRoot,sdkPin,target,cacheRoot,work=null,saveWork,signal,onProgress,onOutput}) {
  if (process.platform !== 'win32' || process.arch !== 'x64') fail('the selected backend requires Windows x64');
  resource=structuredClone(validateResource(resource)); sdkPin=structuredClone(pin(sdkPin,true)); ordinary(resourceRoot,true);
  if (!same(readResource(resourceRoot),resource)) fail('resource differs from staged descriptor');
  const data=provenance(resource,resourceRoot), tool=toolInputs(resourceRoot,data.backend_policy);
  const helper=helperInputs(resourceRoot);
  const resourceHash=crypto.createHash('sha256').update(JSON.stringify(ordered(resource))).digest('hex');
  const record=work === null ? {schema:1,resource_sha256:resourceHash,sdk_sha256:sdkPin.sha256,parts:{}} : structuredClone(work);
  closed(record,['schema','resource_sha256','sdk_sha256','parts'],'backend work');
  if (record.schema !== 1 || record.resource_sha256 !== resourceHash || record.sdk_sha256 !== sdkPin.sha256 || !plain(record.parts) ||
      Object.keys(record.parts).some(name=>!resource.archive.parts.some(p=>p.name===name))) fail('saved backend work differs from selected inputs');
  // All source, descriptor and SDK refusal checks precede acquisition/native calls.
  const sdk=await loadSDK(sdkRoot,sdkPin);
  if (typeof saveWork !== 'function') fail('the existing installation journal is required');
  if (!path.isAbsolute(target)||!path.isAbsolute(cacheRoot)) fail('absolute installation paths required');
  await archiveEnvironment(helper,cacheRoot,path.dirname(target),{signal,onOutput});
  ordinary(cacheRoot,true);
  const rootGuard=root=>{
    const original=fs.statSync(root,{bigint:true});
    return ()=>{ordinary(root,true);const current=fs.statSync(root,{bigint:true});if(current.dev!==original.dev||current.ino!==original.ino)fail('owned directory changed');};
  };
  const cacheGuard=rootGuard(cacheRoot);
  if (!path.isAbsolute(target)) fail('absolute selected installation target required');
  if (fs.existsSync(target)) { ordinary(target,true); if (fs.readdirSync(target).length) fail('nonempty target requires installation-owner cleanup or publish disposition'); }
  else { ordinary(path.dirname(target),true); fs.mkdirSync(target); }
  const targetGuard=rootGuard(target);
  const expanded=[...data.files.values()].reduce((n,p)=>n+p.bytes,resource.provenance.manifest.bytes);
  ensureCapacity(target,cacheRoot,resource.archive.bytes,expanded);
  abort(signal);
  for (const part of resource.archive.parts) await acquirePart({resource,part,record,sdk,cacheRoot,cacheGuard,saveWork,signal,onProgress});
  verifyParts(cacheRoot,resource.archive);
  const first=under(cacheRoot,resource.archive.parts[0].name);
  const childOptions={cwd:cacheRoot,signal,onOutput,helper,backendParent:path.dirname(target)};
  const listing=await archiveChild(tool,['l','-slt','-sccUTF-8',first],childOptions);
  listMembers(listing,data.files,resource.provenance.manifest); verifyParts(cacheRoot,resource.archive);
  const archivedManifest=await archiveChild(tool,['x','-so',first,'manifest.json'],{...childOptions,limit:MANIFEST_LIMIT});
  if (archivedManifest.length !== resource.provenance.manifest.bytes || crypto.createHash('sha256').update(archivedManifest).digest('hex') !== resource.provenance.manifest.sha256) fail('archive manifest differs from admitted metadata');
  verifyParts(cacheRoot,resource.archive); targetGuard(); cacheGuard();
  if (fs.readdirSync(target).length) fail('target changed before extraction');
  await archiveChild(tool,['x','-y','-o'+target,first],{...childOptions,timeout:600000});
  targetGuard(); cacheGuard(); verifyParts(cacheRoot,resource.archive);
  const expected=new Map(data.files); expected.set('manifest.json',resource.provenance.manifest);
  if (!same(treeFiles(target),[...expected.keys()].sort())) fail('extracted file closure differs');
  for (const [name,p] of expected) hashFile(under(target,name),p);
  await boundedNativeChild(helper,['seal-backend','--root',target,'--manifest-sha256',resource.provenance.manifest.sha256,
    '--manifest-bytes',String(resource.provenance.manifest.bytes),'--record',path.join(cacheRoot,'backend-seal.json')],
    {env:{},cwd:helper.directory,signal,onOutput,limit:4096});
  return {manifest:data.manifest,python:path.join(target,'standalone-env','python.exe'),uv:path.join(target,'standalone-env','uv.exe'),
    comfyui:path.join(target,'ComfyUI'),launcher:under(target,data.manifest.oa_launcher.name),work:structuredClone(record),
    correspondingSource:{archive:structuredClone(resource.corresponding_source),data_release:structuredClone(resource.data_release)}};
}

function launchCommand({resourceRoot,target,cacheRoot,port}) {
  const resource=readResource(resourceRoot);provenance(resource,resourceRoot);
  const helper=helperInputs(resourceRoot);
  ordinary(target,true);ordinary(cacheRoot,true);
  if (!path.isAbsolute(target)||!path.isAbsolute(cacheRoot)||!Number.isInteger(port)||port<1||port>65535) fail('invalid owned backend launch');
  hashFile(under(target,'manifest.json'),resource.provenance.manifest);
  const args=['run-backend','--root',target,'--manifest-sha256',resource.provenance.manifest.sha256,
    '--manifest-bytes',String(resource.provenance.manifest.bytes),'--record',path.join(cacheRoot,'backend-seal.json'),
    '--port',String(port),'--state',path.join(cacheRoot,'launcher-state.json')];
  for (const name of ['user','input','output','temporary','models','cache','session']) {
    args.push('--'+name,path.join(cacheRoot,name));
  }
  helper.check();return {cmd:helper.filename,args,cwd:cacheRoot,port,env:{},skipSharedPaths:true};
}
module.exports = {readResource,validateResource,installBackend,launchCommand};
