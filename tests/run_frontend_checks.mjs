import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import {runFrontendTests} from './frontend_regressions.mjs';

const root=new URL('../',import.meta.url);
const source=await fs.readFile(new URL('frontend/js/app.js',root),'utf8');
const result=await runFrontendTests(source);
const record={application_version:source.match(/APP_VERSION\s*=\s*['"]([^'"]+)/)?.[1],...result,
  source_sha256:crypto.createHash('sha256').update(source).digest('hex')};
const target=new URL('docs/修复验证/3.1.0/前端回归.json',root);
await fs.mkdir(new URL('./',target),{recursive:true});
await fs.writeFile(target,JSON.stringify(record,null,2));
console.log(JSON.stringify({total:result.total,all_passed:result.all_passed,
  failures:result.results.filter(item=>!item.passed)},null,2));
process.exitCode=result.all_passed?0:1;
