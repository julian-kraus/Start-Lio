"""Timeline rendering against synthetic live records."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_timeline_uses_records_and_keeps_reject_disabled_without_route():
    script = r'''
const fs=require('fs'),vm=require('vm');
class Element {
 constructor(tag,attrs={}){this.tag=tag;this.attrs=attrs;this.children=[];this.style={};this.className=attrs.class||'';this.disabled=!!attrs.disabled;}
 append(...items){this.children.push(...items.flat().filter(x=>x!==null&&x!==undefined))}
}
global.window={}; global.el=(tag,attrs,...children)=>{const n=new Element(tag,attrs);n.append(...children);return n};
global.sv=(tag,attrs,...children)=>{const n=new Element(tag,attrs);n.append(...children);return n};
global.fmt=x=>x;global.eur=x=>'€'+x;global.select=()=>{};global.setRoute=()=>{};
global.acceptButton=(d)=>new Element('button',{disabled:!d.signer?.name});
vm.runInThisContext(fs.readFileSync('tracker/lookahead_static/timeline.js','utf8'));
const root=new Element('root');const state={clock:{now:'2030-01-01T10:00:00Z'},launch:{deadline:'2030-01-01T12:00:00Z'},
 suppliers:[{supplier_id:'s1',name:'Example supplier'}],plan:{lanes:[{supplier_id:'s1',status:'critical'}]},
 events:[{id:1,supplier_id:'s1',at:'2030-01-01T11:00:00Z',title:'Example event',type:'offer'}],
 decisions:[{decision_id:'d1',status:'open',title:'Example decision',cost:{eur:12},signer:{name:'Example signer'}}]};
window.LOOKAHEAD_VIEWS.timeline(root,state,k=>k);
function all(n){return typeof n==='string'||typeof n==='number'?[String(n)]:[n,...n.children.flatMap(all)]}
const nodes=all(root);for(const value of ['Example supplier','Example event','Example decision','€12'])if(!nodes.includes(value))throw Error('missing '+value);
const reject=nodes.find(x=>typeof x==='object' && x.attrs?.title==='timeline.reject_unavailable');if(!reject||!reject.disabled)throw Error('unsafe reject');
const empty=new Element('root');window.LOOKAHEAD_VIEWS.timeline(empty,{suppliers:[],events:[],decisions:[]},k=>k);
'''
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)
