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
let call;global.api=async(path,body)=>{call={path,body};return {}};global.load=async()=>{};
vm.runInThisContext(fs.readFileSync('tracker/lookahead_static/decision_card.js','utf8'));
const t=x=>x;openRejectDialog({decision_id:'d-1',title:'Example',signer:{name:'A signer'}},t);
function all(n){return typeof n==='string'?[n]:[n,...n.children.flatMap(all)]} const nodes=all(root);
const reason=nodes.find(n=>n.tag==='textarea'), submit=nodes.find(n=>n.tag==='button'&&n.children.includes('decisions.reject'));
if(!submit.disabled)throw Error('empty reason must not submit');reason.value='Not suitable';reason.listeners.input();
(async()=>{await submit.listeners.click();if(call.path!=='/api/lookahead/decisions/d-1/reject'||call.body.approver!=='A signer'||call.body.reason!=='Not suitable')throw Error('incorrect route or body');console.log('ok')})().catch(e=>{console.error(e);process.exitCode=1});
'''
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
