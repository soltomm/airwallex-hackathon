// Rendering regression: every long message must be readable without expansion clicks.
// The state response is a deterministic fixture; no server data is changed.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const base=process.env.CONVERSATION_TEST_URL||'http://127.0.0.1:8025';
if(!['localhost','127.0.0.1'].includes(new URL(base).hostname))throw Error('Use a local test server.');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const page=await browser.newPage(),errors=[];let did;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/state',async route=>{
   const response=await route.fetch(),s=await response.json(),d=s.deals[0];did=d.id;
   d.thread=Array.from({length:5},(_,i)=>({id:'conversation-test-'+i,at:s.clock,from:i%2?'seller':'buyer',
    subject:'Message '+i,text:Array.from({length:16},(_,j)=>`Message ${i}, line ${j}: Full inquiry and response details must remain readable.`).join('\n')+'\n'+'long-reference-'.repeat(40)+'\nEnd of message '+i}));
   await route.fulfill({response,json:s});
  });
  await page.goto(base);await page.waitForFunction(()=>window.Harbour?.state()?.deals.length);
  for(const width of [1440,390]){
   await page.setViewportSize({width,height:900});
   await page.evaluate(id=>window.Harbour.open(id),did);
   await page.locator('#deal [data-tab="conv"]').click();
   const messages=page.locator('#deal .msg .b');assert.equal(await messages.count(),5);
   for(let i=0;i<5;i++){
    const message=messages.nth(i);assert(await message.isVisible(),'Every message must be expanded by default');
    await message.scrollIntoViewIfNeeded();
    assert.equal(await message.evaluate(el=>el.scrollHeight<=el.clientHeight+1),true,'Message text must not be clipped');
    assert.equal(await message.evaluate(el=>el.scrollWidth<=el.clientWidth+1),true,'Long references must wrap');
    assert.match(await message.innerText(),new RegExp('End of message '+(4-i)));
   }
   assert.equal(await page.locator('#deal details.earlier').count(),0);
   if(width===1440)await page.screenshot({path:'/tmp/conversation-full-messages.png'});
   await page.reload();await page.waitForFunction(()=>window.Harbour?.state()?.deals.length);
  }
  assert.deepEqual(errors,[]);console.log('PASS: all long conversation messages are expanded, unclipped and wrapped on desktop/mobile, including after reload.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
