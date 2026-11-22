"use strict";
async function loadStrings(){
  S.strings={...(window.LOOKAHEAD_STRINGS||{})};
  document.documentElement.lang=S.language==="zh"?"zh-Hant":"en";
  try {
    const response=await fetch("/lookahead/static/strings/index.json");
    if(!response.ok)throw new Error(response.statusText);
    const names=await response.json();
    const catalogs=await Promise.all(names.map(async name=>{
      if(!/^[a-z0-9_-]+\.json$/.test(name))throw new Error("Invalid catalog name");
      const r=await fetch("/lookahead/static/strings/"+name);
      if(!r.ok)throw new Error(r.statusText);
      return r.json();
    }));
    for(const catalog of catalogs)Object.assign(S.strings,catalog);
  }catch(error){console.error("Unable to load translations",error)}
}
function t(key){const row=S.strings[key];if(!row)return key;return row[S.language]||row.en||key}
function switchLanguage(language){if(!["en","zh"].includes(language))return;S.language=language;localStorage.setItem("lookahead-language",language);document.documentElement.lang=language==="zh"?"zh":"en";render()}
document.addEventListener("click",event=>{const target=event.target.closest("[data-lang]");if(target)switchLanguage(target.dataset.lang)});
