// Feasibility prototype. One program per process; only NDJSON tool RPC crosses
// the VM boundary. No provider credentials are passed to this process.
import readline from 'node:readline';
import { CodemodeSandbox } from './pinned-source/index.ts';

const input=readline.createInterface({input:process.stdin,terminal:false});
const pending=new Map(), calls=[];
const abort=new AbortController();
let started=false, finished=false, nextId=0, queue=Promise.resolve(), sandbox, programId;
let stopReason=null;
const MAX_VALUE_BYTES=16*1024**2;
const MAX_CALL_BYTES=64*1024;
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
function stop(reason) {stopReason??=reason;abort.abort(new Error(reason));}
input.on('close',()=>{if(!finished)stop('Host input closed');});
process.on('SIGTERM',()=>stop('Worker process cancelled'));
input.on('line', line=>{
  try {
    const message=JSON.parse(line);
    if(!started){started=true;void run(message);return;}
    if(message.program_id!==programId)throw new Error('Wrong program identity');
    if(message.type==='stop'){stop(message.reason||'Host stopped dispatch');return;}
    if(message.type!=='result'||!pending.has(message.id))throw new Error('Unknown child result');
    const handler=pending.get(message.id);pending.delete(message.id);
    handler.row.status=message.status;
    if(message.stop_reason){stop(message.stop_reason);handler.reject(new Error(message.stop_reason));}
    else if(message.error){handler.reject(new Error(message.error));}
    else {handler.resolve(message.value);}
  } catch(error) {stop('Invalid host protocol: '+error.message);}
});

async function run(init){
  programId=init.program_id;
  const cpuStart=process.cpuUsage();
  let timer;
  try {
    if(init.type!=='run'||typeof programId!=='string'||typeof init.code!=='string'
       ||!Array.isArray(init.tools)||!Number.isFinite(init.timeout_ms)||init.timeout_ms<=0)
      throw new Error('Invalid program contract');
    const maxCalls=init.max_calls??150;
    if(!Number.isSafeInteger(maxCalls)||maxCalls<1||maxCalls>150)
      throw new Error('Invalid remaining logical call budget');
    const tools=init.tools.map(({name,description})=>({name,description,execute:(args,{signal})=>{
      if(calls.length>=maxCalls){stop('Logical tool call budget exceeded');throw new Error('Dispatch closed');}
      if(Buffer.byteLength(JSON.stringify(args)??'null')>MAX_CALL_BYTES){
        stop('Child arguments exceed 64 KiB');throw new Error('Dispatch closed');
      }
      const row={id:++nextId,name,args,status:'requested'};calls.push(row);
      emit({type:'requested',program_id:programId,...row});
      const dispatched=queue.then(()=>{
        if(signal.aborted||abort.signal.aborted){row.status='not_executed';throw new Error('Dispatch closed');}
        row.status='running';
        return new Promise((resolve,reject)=>{
          const onAbort=()=>{if(pending.delete(row.id)){
            row.status='cancelled';emit({type:'cancel',program_id:programId,id:row.id});
            reject(new Error('Child cancelled'));
          }};
          signal.addEventListener('abort',onAbort,{once:true});
          pending.set(row.id,{row,resolve:v=>{signal.removeEventListener('abort',onAbort);resolve(v);},
            reject:e=>{signal.removeEventListener('abort',onAbort);reject(e);}});
          emit({type:'call',program_id:programId,id:row.id,name,args});
        });
      });
      queue=dispatched.catch(()=>{});
      return dispatched;
    }}));
    // CPU is measured for this Node process and its VM worker, not the tools
    // executed by the Python harness. Long builds consume the wall budget.
    const cpuMs=init.cpu_ms??10000;
    timer=setInterval(()=>{
      const usage=process.cpuUsage(cpuStart);
      if((usage.user+usage.system)/1000>cpuMs)stop('Program CPU budget exceeded');
    },25);
    sandbox=new CodemodeSandbox({tools,timeoutMs:init.timeout_ms,memoryLimitBytes:256*1024**2});
    emit({type:'ready',program_id:programId,pid:process.pid});
    let result=await sandbox.execute(init.code,{signal:abort.signal});
    await queue;
    // The upstream text() cap does not limit an explicit return value or
    // store writes. They are not carried to another program in SAG.
    const returnBytes=Buffer.byteLength(JSON.stringify(result.value)??'null');
    const storeBytes=Buffer.byteLength(JSON.stringify(result.storeWrites)??'null');
    if(returnBytes>MAX_VALUE_BYTES||storeBytes>MAX_VALUE_BYTES){
      stopReason??='Program return value exceeds 16 MiB';
      result={...result,ok:false,value:undefined,storeWrites:undefined,
        error:{kind:'output_limit',message:stopReason},rejected_return_bytes:returnBytes};
    }
    const usage=process.cpuUsage(cpuStart);
    emit({type:'done',program_id:programId,result,calls,stop_reason:stopReason,
      node_cpu_ms:(usage.user+usage.system)/1000});
  } catch(error) {
    emit({type:'error',program_id:programId,error:String(error.message),calls});
  } finally {
    finished=true;clearInterval(timer);
    await sandbox?.close();input.close();process.stdin.destroy();
  }
}
