// Run against an isolated simulated server. Creates two inquiries; does not reset it.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const base=process.env.VENDOR_TEST_URL||'http://127.0.0.1:8023';
if(!['localhost','127.0.0.1'].includes(new URL(base).hostname))throw Error('Loopback server required.');
(async()=>{
 const b=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const p=await b.newPage({viewport:{width:1440,height:1000}}),errors=[];
  p.on('pageerror',e=>errors.push(e.message));await p.goto(base);
  assert.equal(await p.evaluate(()=>fetch('/api/state').then(r=>r.json()).then(s=>s.provider)),'simulated');
  for(let i=0;i<2;i++){
   await p.locator('#wbSimulate').click();await p.locator('#wbThirdParty').check();
   await p.locator('#wbThirdPartyCategory').selectOption('av');
   if(i===1)await p.locator('#wbThirdPartyQuote').uncheck();
   await p.locator('[data-preset]').first().click();
   await p.locator('#liveDrawer[open]').waitFor();
   await p.waitForFunction(()=>window.Harbour.state()?.deals.some(d=>d.generated&&d.third_party?.requests.length));
   await p.locator('#vendorInquiryOpen').click();
   if(i===0){await p.getByRole('button',{name:'RevOps approve exact quote',exact:true}).waitFor();}
   else{
    await p.getByRole('button',{name:'Generate hypothetical quote response',exact:true}).click();
    await p.getByRole('button',{name:'RevOps approve exact quote',exact:true}).waitFor();
    await p.getByText('Past transactions with this provider',{exact:true}).click();
    await p.locator('[data-provider-history] table').waitFor();
    assert.match(await p.locator('[data-provider-history]').innerText(),/£250.00/);
   }
   await p.getByRole('button',{name:'RevOps approve exact quote',exact:true}).click();
   await p.getByRole('button',{name:'Simulate vendor confirmation',exact:true}).click();
   await p.getByText('Vendor confirmed',{exact:true}).first().waitFor();
   await p.getByRole('link',{name:'Clear Sound AV',exact:true}).click();
   await p.getByRole('heading',{name:'Past subcontracts & charges'}).waitFor();
   assert.match(await p.locator('#vendorPage').innerText(),/£250.00/);
   await p.getByRole('link',{name:'Back to directory'}).click();
   await p.getByRole('link',{name:'Clear Sound AV',exact:true}).click();
   await p.getByRole('heading',{name:'Past subcontracts & charges'}).waitFor();
   if(i===1)await p.screenshot({path:'/tmp/vendor-history.png',fullPage:true});
   await p.getByRole('button',{name:'Overview',exact:true}).click();
  }
  assert.deepEqual(errors,[]);console.log('PASS: new-inquiry quote opt-in/out, simulated response, approval, confirmation, directory profile and RFP history.');
 }finally{await b.close();}
})().catch(e=>{console.error(e);process.exit(1)});
