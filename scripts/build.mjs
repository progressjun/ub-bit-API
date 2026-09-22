import { mkdir, readFile, writeFile, copyFile } from 'node:fs/promises';
await mkdir('dist/server', {recursive:true});
await mkdir('dist/.openai', {recursive:true});
let source = await readFile('worker/index.js','utf8');
for (const [name,file] of [['HTML','web/index.html'],['CSS','web/style.css'],['JS','web/app.js']]) {
  const content = await readFile(file,'utf8');
  source = source.replace(`'__${name}__'`, () => JSON.stringify(content));
}
await writeFile('dist/server/index.js',source);
await copyFile('.openai/hosting.json','dist/.openai/hosting.json');
console.log('Built UBBIT Worker + console');
