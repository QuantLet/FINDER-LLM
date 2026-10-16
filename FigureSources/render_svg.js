const fs = require('fs');
const { chromium } = require('playwright');
(async () => {
  const source = fs.readFileSync(process.argv[2], 'utf8').replace(/<\?xml[^>]*>/, '');
  const width = Number(source.match(/<svg[^>]*\bwidth="([\d.]+)"/)[1]);
  const height = Number(source.match(/<svg[^>]*\bheight="([\d.]+)"/)[1]);
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_EXECUTABLE ? {executablePath:process.env.CHROME_EXECUTABLE} : {})});
  try {
    const page = await browser.newPage({viewport:{width,height},deviceScaleFactor:3});
    await page.route('**/*', route => route.abort());
    await page.setContent(`<html><head><style>html,body{margin:0;padding:0;background:white}${process.argv[2].includes('Workflow') ? 'svg' : 'body>svg'}{display:block}</style></head><body>${source}</body></html>`);
    await page.evaluate(async () => {await Promise.all([document.fonts.load('400 28px Roboto'),document.fonts.load('700 28px Roboto')]);await document.fonts.ready;});
    await page.screenshot({path:process.argv[3]});
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exit(1);});
