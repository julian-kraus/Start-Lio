"""Exercise the weak-spot view with synthetic records, without a browser service."""
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_view_renders_record_values_and_opens_supplier_panel():
    script = r'''
const fs = require('fs'); const vm = require('vm');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.dataset={}; this.style={}; this.attrs={}; this.listeners={}; this.className=''; this.textContent=''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute(k,v) { this.attrs[k]=v; }
  addEventListener(k,f) { this.listeners[k]=f; }
  focus() {}
  remove() { this.removed=true; }
  querySelector(selector) {
    if (selector === '.gn-m b') return this.children.flatMap(x => x.children || []).find(x => x.tagName === 'b' && this.children.some(c => c.className === 'gn-m' && c.children.includes(x))) || null;
    return null;
  }
}
global.window={}; global.document={createElement:t=>new Element(t),createElementNS:(_,t)=>new Element(t)};
global.requestAnimationFrame=()=>{};
for (const file of ['weak.js','suppliers.js','promises.js']) vm.runInThisContext(fs.readFileSync('tracker/lookahead_static/'+file,'utf8'));
const root=new Element('main');
const data={suppliers:[{supplier_id:'s1',name:'Example supplier',score:{value:.8,band:'good',method:'History'},city:'Example city'}],
  plan:{lanes:[{lane_id:'p1',name:'Example part',status:'critical',gap:4}],coverage:{covered:6,needed:10,unit:'units'}},
  promises:[{supplier_id:'s1',what:'Deliver',state:'broken',next_check:'soon',fallback:'Call'}]};
const strings=JSON.parse(fs.readFileSync('tracker/lookahead_static/strings/weak.json','utf8'));
window.LookaheadWeak.render(root,data,k=>strings[k]?.en || k);
function all(n) { return [n,...n.children.flatMap(all)]; }
const nodes=all(root); const texts=nodes.map(x=>x.textContent);
if (!['Example supplier','Example part','−4','6 / 10 covered','Deliver','Call'].every(x=>texts.includes(x))) throw Error('missing record data');
const allButton=nodes.find(x=>x.textContent==='See all suppliers'); allButton.listeners.click();
const after=all(root).map(x=>x.textContent);
if (!after.includes('All suppliers') || !after.includes('Example city') || !after.includes('80%')) throw Error('missing supplier panel');
'''
    subprocess.run(["node", "-e", script], cwd=ROOT, check=True, capture_output=True, text=True)


def test_strings_cover_both_languages():
    strings = json.loads((ROOT / "tracker/lookahead_static/strings/weak.json").read_text())
    assert strings
    assert all(entry.get("en") and entry.get("zh") for entry in strings.values())
