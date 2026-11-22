"use strict";
async function loadStrings(){S.strings=window.LOOKAHEAD_STRINGS||{}}
function t(key){const row=S.strings[key];if(!row)return key;return row[S.language]||row.en||key}
function switchLanguage(language){if(!["en","zh"].includes(language))return;S.language=language;localStorage.setItem("lookahead-language",language);document.documentElement.lang=language==="zh"?"zh":"en";render()}
document.addEventListener("click",event=>{const target=event.target.closest("[data-lang]");if(target)switchLanguage(target.dataset.lang)});
