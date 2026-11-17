import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {runFrontendTests} from './frontend_regressions.mjs';
import {runNumberTests} from './numbers_regressions.mjs';

// 用法：node tests/run_frontend_checks.mjs [输出 JSON 路径]
const root=new URL('../',import.meta.url);
const rootPath=fileURLToPath(root);
const read=async path=>fs.readFile(new URL(path,root),'utf8');
const sha=text=>crypto.createHash('sha256').update(text).digest('hex');
const files=['frontend/js/app.js','frontend/js/drafts.js','frontend/js/workflow.js','frontend/js/device-picker.js',
  'frontend/js/chinese.js','frontend/js/numbers.js','tests/frontend_regressions.mjs','tests/numbers_regressions.mjs'];
const sources={};
for(const file of files)sources[file]=sha(await read(file));
const source=await read('frontend/js/app.js');
let gitCommit='',gitDirty=false;
try{
  gitCommit=execFileSync('git',['rev-parse','HEAD'],{cwd:rootPath,encoding:'utf8'}).trim();
  gitDirty=execFileSync('git',['status','--porcelain'],{cwd:rootPath,encoding:'utf8'}).trim()!=='';
}catch{}
const started=new Date();
const result=await runFrontendTests(source);
const numbers=runNumberTests(await read('frontend/js/numbers.js'));
const results=[...result.results,...numbers.results.map(item=>({...item,suite:'numbers'}))];
const after={};for(const file of files)after[file]=sha(await read(file));
const changed=files.filter(file=>sources[file]!==after[file]);
const record={application_version:source.match(/APP_VERSION\s*=\s*['"]([^'"]+)/)?.[1],
  total:results.length,all_passed:result.all_passed&&numbers.all_passed,failures:results.filter(item=>!item.passed),
  results,source_sha256:sha(source),sources,sources_after:after,source_changed_during_tests:changed,
  usable_as_baseline:result.all_passed&&numbers.all_passed&&!changed.length,numbered_tests:numbers.total,
  git_commit:gitCommit,git_dirty:gitDirty,started_at:started.toISOString(),
  finished_at:new Date().toISOString()};
const target=new URL(process.argv[2]??'docs/修复验证/3.1.0/前端回归.json',root);
await fs.mkdir(new URL('./',target),{recursive:true});
await fs.writeFile(target,JSON.stringify(record,null,2));
console.log(JSON.stringify({total:record.total,all_passed:record.all_passed,failures:record.failures},null,2));
process.exitCode=record.all_passed?0:1;
