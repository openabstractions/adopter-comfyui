// Actual selected extension module, evaluated with inert browser/native owners.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { graphView } from "./oa_appmodel_graph.mjs";

// shape "1.55" is frontend 1.55.x's app; "1.58" keeps widget values behind
// getters and setters and has no app.ui legacy menu; "bare" exposes no Vue
// application, so no queue store.
async function fixture(credential = true, brokenGraph = false, origin = "http://127.0.0.1:8188", shape = "1.55",
  hash = credential ? "#oa-appmodel=generation00000001" : "") {
  const imports = [], listeners = new Map(), sockets = [], posts = [];
  let bootstrap = null;
  const callback = function (value) { this.value = Math.round(value); };
  const options = {min:1,max:10000,step:10,step2:1,precision:0};
  let stored_value = 20, setterThrows = false;
  const widget = shape === "1.58"
    ? {name:"steps",type:"number",callback,options,get value(){return stored_value;},set value(v){stored_value=v;}}
    : {name:"steps",type:"number",value:20,callback,options};
  const node = {id:7,type:"KSampler",comfyClass:"KSampler",inputs:[],widgets:[widget],pos:new Float32Array([10,20])};
  const graph = {_nodes:[node],getNodeById:()=>node}; node.graph=graph;
  let commits=0, id=0, confirmation=true, confirms=0, heartbeat;
  const app = {isGraphReady:true,rootGraph:graph,graph,canvas:{graph,ds:{scale:1,offset:[0,0]},
    canvas:{getBoundingClientRect:()=>({left:0,top:0})}},
    extensionManager:{workflow:{isBusy:false,activeWorkflow:{changeTracker:{beforeChange(){},afterChange(){
      commits++; listeners.get("graphChanged")?.();
    }}}}},registerExtension(value){this.extension=value;}};
  if (shape !== "1.58") app.ui = {autoQueueEnabled:false};
  // brokenGraph: the frontend has no graph yet.
  if (brokenGraph) Object.assign(app, {isGraphReady:false,rootGraph:undefined,graph:undefined});
  const queue={mode:"disabled"};
  const pinia={_s:new Map([["queueSettingsStore",queue]])};
  widget.callback = function (value) { callback.call(this, value); if (setterThrows) throw Error("lost after write"); };
  class Socket {
    static OPEN=1;static CLOSED=3;
    constructor(url,protocols){this.url=url;this.protocols=protocols;this.readyState=1;this.sent=[];sockets.push(this);}
    send(value){this.sent.push(JSON.parse(value));}
    close(){this.readyState=3;this.onclose?.();}
  }
  let stored=credential?JSON.stringify({credential:"a".repeat(32),generation:"generation00000001"}):null;
  const notices=[];
  const root = shape === "bare" ? null : {__vue_app__:{config:{globalProperties:{$pinia:pinia}}}};
  const location={origin,host:origin.replace("http://",""),hash,pathname:"/",search:""};
  const globals={URL,TextEncoder,WebSocket:Socket,location,history:{replaceState(){location.hash="";}},innerWidth:1000,innerHeight:800,
    document:{body:{append:value=>notices.push(value)},querySelector:selector=>selector==="#vue-app"?root:null,
      createElement:tag=>({tag,style:{},children:[],
      append(...children){this.children.push(...children);},setAttribute(){},remove(){this.removed=true;}})},
    confirm:()=>{confirms++;return confirmation;},
    crypto:{randomUUID:()=>String(++id).padStart(32,"0")},
    sessionStorage:{getItem:()=>stored,setItem:(_,value)=>{stored=value;},removeItem:()=>{stored=null;}},
    fetch:async(path,init)=>{posts.push({path:String(path),init});if(!bootstrap)throw Error("no fetch");return bootstrap;},setInterval:fn=>{heartbeat=fn;return 1;},clearInterval:()=>{}};
  const prompts=[];
  const api={addEventListener:(name,fn)=>listeners.set(name,fn),
    async queuePrompt(number,data){prompts.push(data);return {prompt_id:"prompt"+String(prompts.length).padStart(12,"0"),number};}};
  app.queuePrompt=async()=>{await api.queuePrompt(0,{output:{}});return true;};
  const context=vm.createContext(globals);
  async function module(values){const m=new vm.SyntheticModule(Object.keys(values),function(){for(const[k,v]of Object.entries(values))this.setExport(k,v);},{context});await m.link(()=>{});await m.evaluate();return m;}
  const source=fs.readFileSync(new URL("./oa_appmodel.js",import.meta.url),"utf8");
  const main=new vm.SourceTextModule(source,{context,identifier:"file:///selected/oa_appmodel.js",
    initializeImportMeta(meta){meta.url="file:///selected/oa_appmodel.js";},
    async importModuleDynamically(path){imports.push(path);throw Error("no dynamic import");}});
  await main.link(path=>module(path.endsWith("scripts/app.js")?{app}:path.endsWith("scripts/api.js")?
    {api}:{graphView}));
  await main.evaluate(); app.extension.setup();
  for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));
  return {app,widget,queue,pinia,sockets,imports,notices,listeners,posts,prompts,location,commits:()=>commits,
    bootstrap:value=>{bootstrap=value;},stored:()=>stored,
    confirmation:value=>{confirmation=value;},confirms:()=>confirms,
    setterThrows:()=>{setterThrows=true;},heartbeat:()=>heartbeat()};
}
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
const edit=(state,value,sequence=0)=>({operation:"edit",epoch:state.epoch,sequence,
  arguments:{ops:[{op:"set_widget",node:"7",widget:"steps",value}]}});
for (const shape of ["1.55", "1.58"]) test(`${shape}: the entry imports no frontend module and returns one correlated edit`,async()=>{
  const f=await fixture(true,false,"http://127.0.0.1:8188",shape),ws=f.sockets[0];
  assert.deepEqual(f.imports,[]);
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  const state=ws.sent[0].projection;
  assert.deepEqual(state.graph.nodes.map(node=>[node.id,node.widgets.map(w=>[w.name,w.type,w.value])]),[["7",[["steps","int",20]]]]);
  ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000001",request:edit(state,24)})});
  await settle();
  assert.equal(f.widget.value,24);assert.equal(f.commits(),1);
  assert.deepEqual(ws.sent.map(v=>v.type),["projection","result"]);
  assert.equal(ws.sent[1].outcome,"applied");assert.equal(ws.sent[1].projection.sequence,1);
  assert.deepEqual(ws.sent[1].answer,{applied:1});
});
test("bare: with no queue store the canvas is projected, edited and revealed",async()=>{
  const f=await fixture(true,false,"http://127.0.0.1:8188","bare"),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  const state=ws.sent[0].projection;
  ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000001",request:edit(state,24)})});
  await settle();
  assert.equal(ws.sent[1].outcome,"applied");assert.equal(f.widget.value,24);
  f.app.canvas.centerOnNode=()=>{};
  ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000002",request:{operation:"reveal",epoch:state.epoch,sequence:1,arguments:{node:"7"}}})});
  await settle();
  assert.equal(ws.sent[2].outcome,"applied");
});
test("run queues the canvas through the page's Queue and answers the prompt with the view",async()=>{
  const f=await fixture(),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  const state=ws.sent[0].projection;
  ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000001",request:{operation:"run",epoch:state.epoch,sequence:0,arguments:{}}})});
  await settle();
  assert.equal(f.prompts.length,1);
  assert.deepEqual([ws.sent[1].type,ws.sent[1].outcome,ws.sent[1].answer],["result","applied",{prompts:["prompt000000000001"]}]);
  assert.equal(ws.sent[1].projection.epoch,state.epoch);
});
test("a frontend that skips the load hooks starts a new epoch on its next change",async()=>{
  const f=await fixture(),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  const first=ws.sent[0].projection.epoch;
  f.listeners.get("graphCleared")();
  assert.equal(ws.sent.at(-1).type,"retired");
  f.listeners.get("graphChanged")();
  assert.equal(ws.sent.at(-1).type,"projection");assert.notEqual(ws.sent.at(-1).projection.epoch,first);
  assert.equal(ws.sent.at(-1).projection.sequence,0);
});
test("a graphChanged that changed nothing sends no projection; a person's edit sends the next sequence",async()=>{
  const f=await fixture(),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  f.listeners.get("graphChanged")();
  assert.equal(ws.sent.length,1);
  f.widget.value=31;f.listeners.get("graphChanged")();
  assert.equal(ws.sent.length,2);assert.equal(ws.sent[1].projection.sequence,1);
});
test("modern auto queue refuses an edit; a missing queue store edits",async()=>{
  for(const missing of [false,true]){
    const f=await fixture(),ws=f.sockets[0];
    ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
    const state=ws.sent[0].projection;
    if(missing)f.pinia._s.clear();else f.queue.mode="change";
    ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000001",request:edit(state,24)})});
    await settle();
    assert.equal(ws.sent[1].outcome,missing?"applied":"refused");assert.equal(f.widget.value,missing?24:20);
  }
});
test("the page joins its own ComfyUI listener on any loopback port and nothing else",async()=>{
  const desktop=await fixture(true,false,"http://127.0.0.1:8000");
  assert.equal(desktop.sockets.length,1);
  assert.equal(desktop.sockets[0].url,"ws://127.0.0.1:8000/oa/appmodel/ws");
  for(const origin of ["http://localhost:8188","http://127.0.0.1:0","https://127.0.0.1:8188","http://192.168.1.2:8188"])
    assert.equal((await fixture(true,false,origin)).sockets.length,0,origin);
});
test("Desktop window: the arm event's empty bootstrap starts a session with no Close control",async()=>{
  const f=await fixture(false,false,"http://127.0.0.1:8000");
  assert.equal(f.sockets.length,0);
  f.bootstrap({ok:true,json:async()=>({credential:"b".repeat(32),generation:"generation00000002"})});
  f.listeners.get("oa-appmodel-arm")();await settle();
  assert.equal(f.posts.length,1);
  assert.equal(f.posts[0].path,"/oa/appmodel/bootstrap");
  assert.equal(f.posts[0].init.method,"POST");assert.equal(f.posts[0].init.body,undefined);
  assert.equal(f.sockets.length,1);
  assert.deepEqual([...f.sockets[0].protocols],["oa-comfy-graph-v1","b".repeat(32)]);
  assert.equal(f.stored(),null);
  const ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000002"})});
  assert.equal(ws.sent[0].type,"projection");assert.equal(f.notices.length,0);
  f.listeners.get("oa-appmodel-arm")();await settle();
  assert.equal(f.posts.length,1,"a live session ignores further arm events");
  ws.close();f.listeners.get("oa-appmodel-arm")();await settle();
  assert.equal(f.posts.length,2);assert.equal(f.sockets.length,2,"the next arm starts a fresh session");
});
test("a page the bridge refuses after the arm event stays inert",async()=>{
  const f=await fixture(false,false,"http://127.0.0.1:8000");
  f.bootstrap({ok:false,json:async()=>({})});
  f.listeners.get("oa-appmodel-arm")();await settle();
  assert.equal(f.posts.length,1);assert.equal(f.sockets.length,0);assert.equal(f.stored(),null);
  const other=await fixture(false,false,"http://localhost:8000");
  other.listeners.get("oa-appmodel-arm")();await settle();
  assert.equal(other.posts.length,0);
});
test("direct navigation stays detached even with a saved native credential",async()=>{
  for (const [credential, hash] of [[false,""],[true,""],[true,"#oa-appmodel=generation00000002"]]) {
    const f=await fixture(credential,false,"http://127.0.0.1:8188","1.55",hash);
    assert.equal(f.sockets.length,0);
    assert.equal(f.imports.length,0);
    assert.equal(f.notices.length,0);
    assert.equal(f.stored(),null);
  }
  const opened=await fixture();
  assert.equal(opened.sockets.length,1);
  assert.equal(opened.location.hash,"");
  assert.equal(opened.stored(),null);
});
test("local close requires a trusted click and affirmative confirmation, never a graph argument",async()=>{
  const f=await fixture(),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  const [button,status]=f.notices[0].children;
  button.onclick({isTrusted:false});assert.equal(f.confirms(),0);assert.equal(ws.sent.length,1);
  f.confirmation(false);button.onclick({isTrusted:true});assert.equal(f.confirms(),1);assert.equal(ws.sent.length,1);
  const state=ws.sent[0].projection;
  ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000001",request:{operation:"shutdown",
    epoch:state.epoch,sequence:0,arguments:{}}})});
  await settle();
  assert.equal(ws.sent.at(-1).outcome,"invalid");assert.equal(f.confirms(),1);
  f.confirmation(true);button.onclick({isTrusted:true});
  assert.deepEqual(ws.sent.at(-1),{type:"shutdown",generation:"generation00000001",page:"page000000000001"});
  assert.equal(button.disabled,true);button.onclick({isTrusted:true});assert.equal(f.confirms(),2);
  ws.onmessage({data:JSON.stringify({type:"shutdown",outcome:"requested"})});
  assert.equal(status.textContent,"Stop requested. Waiting for ComfyUI to close…");
  ws.close();assert.equal(status.textContent,"Connection closed. Stop outcome is unconfirmed here.");
});
test("tab close and workflow reload never originate a shutdown event",async()=>{
  const f=await fixture(),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  f.app.extension.beforeLoadGraph();ws.close();
  assert.equal(ws.sent.filter(value=>value.type==="shutdown").length,0);assert.equal(f.confirms(),0);
});
test("authenticated local close remains available with no initialized graph",async()=>{
  const f=await fixture(true,true),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  assert.equal(ws.sent.length,0);
  f.notices[0].children[0].onclick({isTrusted:true});
  assert.deepEqual(ws.sent,[{type:"shutdown",generation:"generation00000001",page:"page000000000001"}]);
});

test("uncertain edit retires graph while original page heartbeat and confirmed Close survive",async()=>{
  const f=await fixture(),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  const state=ws.sent[0].projection;
  f.setterThrows();
  ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000001",request:edit(state,24)})});
  await settle();
  assert.equal(f.widget.value,24);assert.equal(ws.sent.at(-1).outcome,"unknown");assert.equal(ws.readyState,1);
  f.app.extension.afterLoadGraph();f.heartbeat();assert.equal(ws.sent.at(-1).type,"ping");
  ws.onmessage({data:JSON.stringify({type:"request",id:"request0000000002",request:edit(state,30)})});
  await settle();
  assert.equal(ws.sent.at(-1).outcome,"unavailable");assert.equal(f.widget.value,24);
  f.notices[0].children[0].onclick({isTrusted:true});assert.equal(ws.sent.at(-1).type,"shutdown");
});

test("absolute graph retirement notice preserves only original page Close and heartbeat",async()=>{
  const f=await fixture(),ws=f.sockets[0];
  ws.onmessage({data:JSON.stringify({type:"ready",page:"page000000000001",generation:"generation00000001"})});
  const count=ws.sent.length;
  ws.onmessage({data:JSON.stringify({type:"graph_retired"})});
  f.app.extension.afterLoadGraph();assert.equal(ws.sent.length,count);
  f.heartbeat();assert.equal(ws.sent.at(-1).type,"ping");assert.equal(ws.readyState,1);
  f.notices[0].children[0].onclick({isTrusted:true});assert.equal(ws.sent.at(-1).type,"shutdown");
});
