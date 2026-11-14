// 从 stdin 读 [{index, expr, display}]，用 KaTeX 试渲染，把有问题的结果写到 stdout。
const katexPath = process.argv[2];
if (!katexPath) {
  console.error('missing katex path');
  process.exit(2);
}
const katex = require(katexPath);

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  let items;
  try {
    items = JSON.parse(input);
  } catch (e) {
    console.error('invalid input json: ' + e.message);
    process.exit(2);
  }
  const out = [];
  for (const item of items) {
    const warnings = new Set();
    let status = 'ok';
    let message = '';
    try {
      katex.renderToString(item.expr, {
        displayMode: !!item.display,
        throwOnError: true,
        strict: (code, msg) => { warnings.add(code + ': ' + msg); return 'ignore'; },
      });
    } catch (e) {
      status = 'error';
      message = String(e.message).split('\n')[0];
    }
    if (warnings.size) {
      if (status === 'ok') status = 'warn';
      message = (message ? message + ' | ' : '') + [...warnings].join(' | ');
    }
    if (status !== 'ok') out.push({ index: item.index, status, message });
  }
  process.stdout.write(JSON.stringify(out));
});
