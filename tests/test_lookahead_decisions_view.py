"""Decision UI actions use the existing Lookahead endpoints."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_reject_dialog_submits_named_signer_and_reason_only():
    script = r'''
const fs=require('fs'),vm=require('vm');
class E { constructor(tag,attrs={}){this.tag=tag;this.attrs=attrs;this.children=[];this.listeners={};this.value='';this.disabled=!!attrs.disabled;this.textContent='';}
 append(...items){this.children.push(...items.flat().filter(x=>x!==null&&x!==undefined))} replaceChildren(...items){this.children=items}
 addEventListener(k,f){this.listeners[k]=f} focus(){} }
global.window={};const root=new E('root');global.$=id=>root;global.el=(tag,attrs,...children)=>{const e=new E(tag,attrs);e.append(...children);return e};
let call;global.api=async(path,body)=>{if(body){call={path,body};return {}}return {signer:{name:'A signer'},approval:{status:'resolved'}}};global.load=async()=>{};
vm.runInThisContext(fs.readFileSync('tracker/lookahead_static/decision_card.js','utf8'));
const t=x=>x;openRejectDialog({decision_id:'d-1',title:'Example',signer:{name:'A signer'}},t);
function all(n){return typeof n==='string'?[n]:[n,...n.children.flatMap(all)]} const nodes=all(root);
const reason=nodes.find(n=>n.tag==='textarea'), submit=nodes.find(n=>n.tag==='button'&&n.children.includes('decisions.regenerate'));
if(!submit)throw Error('reject action must offer regeneration');
if(!submit.disabled)throw Error('empty reason must not submit');reason.value='Not suitable';reason.listeners.input();
(async()=>{await submit.listeners.click();if(call.path!=='/api/lookahead/decisions/d-1/reject'||call.body.approver!=='A signer'||call.body.reason!=='Not suitable')throw Error('incorrect route or body');console.log('ok')})().catch(e=>{console.error(e);process.exitCode=1});
'''
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


def test_actions_wait_for_routed_signer():
    script = r'''
const fs=require('fs'),vm=require('vm');
class E {constructor(tag,a={}){this.tag=tag;this.attrs=a;this.children=[];this.disabled=!!a.disabled;this.textContent=''} append(...v){this.children.push(...v)} replaceChildren(...v){this.children=v}}
global.window={};global.document={createTextNode:x=>x};global.el=(tag,a,...v)=>{const n=new E(tag,a);n.append(...v);return n};
let resolve,calls=0;global.api=()=>++calls===1?new Promise(r=>resolve=r):Promise.resolve({decision_id:'d1',signer:{name:'Routed'},approval:{status:'resolved'}});global.acceptButton=(d)=>{const n=new E('button');n.routed=d;return n};
vm.runInThisContext(fs.readFileSync('tracker/lookahead_static/decision_card.js','utf8'));
const error=new E('p'),signer=new E('span');const actions=window.decisionActions({decision_id:'d1',signer:{name:'Untrusted'}},x=>x,error,signer);
if(!actions.children.every(x=>x.disabled))throw Error('actions enabled before routing');
resolve({decision_id:'d1',signer:{name:'Routed'},approval:{status:'resolved'}});
setImmediate(()=>{if(!signer.children.includes('Routed')||actions.children[1].disabled)throw Error('routing not used')});
'''
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
