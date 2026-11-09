/* Payer awareness and the hotel-only FX policy directory. Execution stays on the server. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let catalog=null, loading=null, returnFocus=null;
  const call=async(path,body)=>{const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw Error(j.error||'Could not load FX data.');return j;};
  async function load(){if(catalog)return catalog;if(!loading)loading=call('/api/fx/countries').then(x=>catalog=x).finally(()=>loading=null);return loading;}
  const badge=c=>`<span class="fx-risk ${esc(c?.tier||'review')}">${c?.tier==='stable'?'Stable currency':c?.tier==='unstable'?'Unstable currency':'Review needed'}</span>`;
  function summary(fx){
    const p=fx?.origin, receipt=fx?.latest;
    if(!p)return '<span class="muted">Not recorded</span><span class="wb-source">Payer origin unknown</span>'+(receipt?`<span class="wb-next">${receipt.simulated?'Simulated FX: ':'FX: '}${esc(receipt.status)} ${esc(receipt.currency)}</span>`:'');
    return `<b>${esc(p.country)}</b><span class="wb-source">${esc(p.local_currency)} · ${esc(p.classification?.tier||'review')}</span><span class="wb-next">${receipt?`${receipt.simulated?'Simulated FX: ':'FX: '}${esc(receipt.status)} ${esc(receipt.currency)}`:'Awaiting verified funds'}</span>`;
  }
  function options(code=''){return '<option value="">Choose payer country</option>'+catalog.countries.map(c=>`<option value="${c.code}" ${code===c.code?'selected':''}>${esc(c.name)}</option>`).join('');}
  function currencies(country, selected=''){
    const list=catalog.countries.find(c=>c.code===country)?.currencies||[];
    return (list.length!==1?'<option value="">Choose local currency</option>':'')+list.map(c=>`<option value="${c.code}" ${c.code===selected?'selected':''}>${c.code} · ${esc(c.name)}</option>`).join('');
  }
  function drawDirectory(){
    const query=$('fxSearch').value.toLowerCase(), tier=$('fxFilter').value;
    const rows=catalog.countries.filter(c=>`${c.name} ${c.code} ${c.currencies.map(x=>x.code+' '+x.name).join(' ')}`.toLowerCase().includes(query) && (!tier||c.currencies.some(x=>x.tier===tier)));
    $('fxCountryCount').textContent=`${rows.length} of ${catalog.countries.length} countries and territories`;
    $('fxCountryRows').innerHTML=rows.map(c=>`<tr><td>${esc(c.name)}<span class="wb-source">${c.code}</span>${c.historical_macro?`<span class="wb-source">Historical CPI (${c.historical_macro.year}): ${c.historical_macro.consumer_inflation_pct}%</span>`:''}</td><td>${c.currencies.map(x=>`<div class="fx-directory-currency"><b>${x.code}</b> ${esc(x.name)}<br>${badge(x)}<p class="wb-sub">${esc(x.reason)}</p></div>`).join('')||'<span class="muted">No recorded national tender · review</span>'}</td></tr>`).join('')||'<tr><td colspan="2">No matching country or currency.</td></tr>';
  }
  async function directory(){
    returnFocus=document.activeElement;
    const dialog=$('fxDirectory');if(!dialog.open)dialog.showModal();
    $('fxDirectoryBody').innerHTML='<p role="status">Loading country and currency policy…</p>';
    try{
      await load();
      $('fxDirectoryBody').innerHTML=`<h2 id="fxDirectoryTitle" tabindex="-1">A world of currencies.</h2><p class="wb-lede">Directional hotel policy: hold stable foreign currencies, convert unstable ones to GBP after verified receipt. GBP is already the hotel currency.</p>
      <div class="wb-demo-note">Policy reviewed ${esc(catalog.policy.reviewed_on)} · Sandbox execution only. Labels are directional judgments, not guarantees. Convertibility or provider restrictions can require review.</div>
      <div class="wb-terms"><label class="wb-field">Country or currency<input type="search" id="fxSearch" placeholder="Search United States, USD…"></label><label class="wb-field">Policy tag<select id="fxFilter"><option value="">All currencies</option><option value="stable">Stable</option><option value="unstable">Unstable</option><option value="review">Review needed</option></select></label></div>
      <p class="wb-sub" id="fxCountryCount" role="status"></p><div class="fx-country-table"><table class="wb-table"><thead><tr><th>Country / territory</th><th>Local currencies & policy</th></tr></thead><tbody id="fxCountryRows"></tbody></table></div>
      <details><summary>Policy basis and data coverage</summary><p>${esc(catalog.policy.classification)}</p><p>${esc(catalog.coverage)} Currency mapping dated ${esc(catalog.as_of)}. Tags belong to currencies, not nationalities. A country may use multiple currencies.</p><p>Risk flags consider persistent inflation, devaluation, FX restrictions and market liquidity. The remaining known currencies use a stable baseline. The policy expires after ${catalog.policy.valid_days} days and then requires review before automatic foreign FX.</p><p>Sources: <a href="${esc(catalog.sources.countries)}" target="_blank" rel="noreferrer">ISO country data</a> · <a href="${esc(catalog.sources.tender)}" target="_blank" rel="noreferrer">Unicode currency mapping</a>. Bundled World Bank inflation context ends in ${catalog.macro_source.latest_year}; it is historical context, not a current macro feed.</p></details>`;
      $('fxSearch').oninput=drawDirectory;$('fxFilter').onchange=drawDirectory;drawDirectory();$('fxDirectoryTitle').focus();
    }catch(e){$('fxDirectoryBody').textContent=e.message;}
  }
  async function mount(d){
    if(!$('deal')||!d)return;
    const fx=d.payer_fx||{}, p=fx.origin, latest=fx.latest;
    const card=document.createElement('section');card.className='card';card.id='fxPayerCard';card.setAttribute('aria-label','Payer origin and automated FX');
    card.innerHTML=`<div class="label"><span>Payer origin & automated FX</span><button class="btn-ghost fx-directory-button">Country directory ↗</button></div>
      <p class="small muted">Local currency is awareness only. FX uses the currency actually received, not the payer’s country.</p>
      <form id="fxPayerForm"><div class="wb-terms"><label class="wb-field">Payer country<select id="fxPayerCountry" required><option>Loading countries…</option></select></label><label class="wb-field">Local currency<select id="fxPayerCurrency" required><option>Choose country first</option></select></label></div><button class="btn-p" id="fxSavePayer">Save payer origin</button><p id="fxPayerNotice" class="small" role="status"></p></form>
      ${p?`<p>${esc(p.country)} · ${esc(p.local_currency)} ${badge(p.classification)}</p>`:''}
      ${latest?`<div class="fx-outcome"><b>${latest.simulated?'Simulated FX':'Sandbox FX'} · ${esc(latest.status)}</b><p>${esc(latest.currency)} ${esc(latest.amount)} · ${esc(latest.reason)}</p>${latest.conversion_id?`<small>Provider conversion: ${esc(latest.conversion_id)}</small>`:''}</div>`:'<p class="small muted">Awaiting verified funds. Current card checkout charges GBP; no foreign conversion is needed for those receipts.</p>'}
      ${fx.reference?`<details><summary>Foreign bank receipt monitoring</summary><p class="small">A sandbox hotel Global Account receipt must be a settled credit with this exact reference:</p><code class="fx-reference">${esc(fx.reference)}</code><p class="small muted">The agent checks every 30 seconds. Bank receipts here are treasury funds; they do not confirm a group booking or replace its GBP card deposit.</p>${fx.scan_error?`<p class="small">${esc(fx.scan_error)}</p>`:''}<div class="row"><button type="button" id="fxScan">Check receipts now</button>${p&&window.Harbour.state()?.provider==='simulated'?'<button type="button" id="fxSimulate">Simulate 1,000 local-currency receipt</button>':''}</div>${window.Harbour.state()?.provider==='simulated'?'<p class="small muted">FX test fixture only. Illustrative fixed rate; no money moves and no booking is confirmed.</p>':''}</details>`:''}`;
    $('fxPayerCard')?.remove();$('deal').append(card);
    card.querySelector('.fx-directory-button').onclick=directory;
    try{await load();if(!card.isConnected)return;$('fxPayerCountry').innerHTML=options(p?.country_code);$('fxPayerCurrency').innerHTML=currencies(p?.country_code,p?.local_currency);
      $('fxPayerCountry').onchange=()=>{$('fxPayerCurrency').innerHTML=currencies($('fxPayerCountry').value);};
    }catch(e){if(card.isConnected)$('fxPayerNotice').textContent=e.message;}
    if(!card.isConnected)return;
    $('fxPayerForm').onsubmit=async e=>{e.preventDefault();const button=$('fxSavePayer');button.disabled=true;try{await call(`/api/deals/${d.id}/payer`,{country_code:$('fxPayerCountry').value,currency:$('fxPayerCurrency').value});await window.Harbour.reload();const updated=window.Harbour.state().deals.find(x=>x.id===d.id);await mount(updated);}catch(err){$('fxPayerNotice').textContent=err.message;}finally{if(button.isConnected)button.disabled=false;}};
    for(const [id,path] of [['fxScan','/api/fx/scan'],['fxSimulate',`/api/deals/${d.id}/fx_simulate`]]){
      if($(id))$(id).onclick=async()=>{const button=$(id);button.disabled=true;try{const result=await call(path,{});if(result.error)throw Error(result.error);await window.Harbour.reload();await mount(window.Harbour.state().deals.find(x=>x.id===d.id));}catch(err){$('fxPayerNotice').textContent=err.message;}finally{if(button.isConnected)button.disabled=false;}};
    }
  }
  document.addEventListener('click',e=>{if(e.target.closest('#fxDirectoryOpen'))directory();});
  $('fxDirectory').addEventListener('close',()=>{if(returnFocus?.isConnected)returnFocus.focus();else $('wbRules').focus();});
  window.FX={summary,mount,directory};
})();
