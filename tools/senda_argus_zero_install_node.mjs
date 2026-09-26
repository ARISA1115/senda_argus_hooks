#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(SCRIPT_DIR, '..');
const TARGET_PACKAGES = ['openai', '@anthropic-ai/sdk', 'ollama', '@modelcontextprotocol/sdk', '@openai/agents', '@langchain/core', '@langchain/langgraph', 'llamaindex', 'ai'];
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0;
const home = os.homedir();
const defaults = {
  runtimeDir: isRoot ? '/opt/senda-argus/node' : path.join(home, '.local', 'share', 'senda-argus', 'node'),
  configFile: isRoot ? '/etc/senda-argus/node.env' : path.join(home, '.config', 'senda-argus', 'node.env'),
  stateFile: isRoot ? '/var/lib/senda-argus/node-zero-code.json' : path.join(home, '.local', 'state', 'senda-argus', 'node-zero-code.json'),
  profileFile: isRoot ? '/etc/profile.d/senda-argus-node.sh' : path.join(home, '.config', 'senda-argus', 'node-profile.sh'),
};

function die(msg, code = 1) { console.error(`[senda-argus] ${msg}`); process.exit(code); }
function say(msg) { console.log(`[senda-argus] ${msg}`); }
function readText(file) { try { return fs.readFileSync(file, 'utf8'); } catch { return ''; } }
function writeFile(file, text, mode = 0o644) { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, text, { mode }); try { fs.chmodSync(file, mode); } catch {} }
function exists(file) { try { return fs.existsSync(file); } catch { return false; } }
function run(cmd, args = [], opts = {}) { return spawnSync(cmd, args, { encoding: 'utf8', stdio: opts.inherit ? 'inherit' : 'pipe', ...opts }); }
function hasSystemd() { return exists('/run/systemd/system') && run('systemctl',['--version']).status === 0; }
function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) { out._.push(a); continue; }
    const key = a.slice(2);
    if (['all','yes','restart','profile','json','dry-run','remove-runtime','remove-config','force'].includes(key)) out[key] = true;
    else {
      const v = argv[++i]; if (v === undefined) die(`missing value for --${key}`);
      if (out[key] === undefined) out[key] = v; else out[key] = Array.isArray(out[key]) ? [...out[key], v] : [out[key], v];
    }
  }
  return out;
}
function arr(v) { return v === undefined ? [] : Array.isArray(v) ? v : [v]; }
function shellQuote(v) { return `'${String(v).replace(/'/g, `'"'"'`)}'`; }
function systemdQuote(v) { return String(v).replace(/\\/g,'\\\\').replace(/"/g,'\\"'); }
function nodeVersion(exe) {
  const r = run(exe, ['--version']);
  if (r.status !== 0) return null;
  const m = String(r.stdout).trim().match(/^v(\d+)\.(\d+)\.(\d+)/);
  if (!m) return null;
  return { raw: m[0], major:+m[1], minor:+m[2], patch:+m[3], supported:(+m[1] > 18 || (+m[1] === 18 && +m[2] >= 19)) };
}
function isNodeExe(exe) { const b = path.basename(exe || ''); return b === 'node' || /^node(js)?\d*$/.test(b); }

function systemdUnitFromCgroup(text) {
  for (const line of text.split(/\r?\n/)) {
    const parts = line.split(':'); const cg = parts.slice(2).join(':');
    const comps = cg.split('/').filter(Boolean);
    for (let i = comps.length - 1; i >= 0; i--) if (comps[i].endsWith('.service')) return comps[i];
  }
  return null;
}

function scanProcesses() {
  const out = [];
  let entries = [];
  try { entries = fs.readdirSync('/proc', { withFileTypes:true }); } catch { return out; }
  for (const ent of entries) {
    if (!ent.isDirectory() || !/^\d+$/.test(ent.name)) continue;
    const pid = +ent.name; const base = `/proc/${pid}`;
    let exe = ''; try { exe = fs.readlinkSync(`${base}/exe`); } catch { continue; }
    const cmdline = readText(`${base}/cmdline`).replace(/\0/g, ' ').trim();
    if (!isNodeExe(exe)) continue;
    let cwd = ''; try { cwd = fs.readlinkSync(`${base}/cwd`); } catch {}
    const cgroup = readText(`${base}/cgroup`);
    const service = systemdUnitFromCgroup(cgroup);
    const version = isNodeExe(exe) ? nodeVersion(exe) : null;
    out.push({ pid, exe, version, cwd, cmdline, service });
  }
  return out;
}

function scanNodeBinaries() {
  const candidates = new Set();
  for (const dir of String(process.env.PATH || '').split(path.delimiter)) if (dir) for (const n of ['node','nodejs']) candidates.add(path.join(dir,n));
  for (const p of ['/usr/bin/node','/usr/local/bin/node','/snap/bin/node']) candidates.add(p);
  const nvm = path.join(home,'.nvm','versions','node');
  try { for (const v of fs.readdirSync(nvm)) candidates.add(path.join(nvm,v,'bin','node')); } catch {}
  const out=[];
  for (const c of candidates) if (exists(c)) { const version=nodeVersion(c); if (version) out.push({exe:c,version}); }
  return [...new Map(out.map(x=>[x.exe,x])).values()];
}

function dependencyHits(pkg) {
  const deps = { ...(pkg.dependencies||{}), ...(pkg.optionalDependencies||{}), ...(pkg.peerDependencies||{}), ...(pkg.devDependencies||{}) };
  return TARGET_PACKAGES.filter(k => Object.prototype.hasOwnProperty.call(deps,k));
}
function scanApps(roots, maxDepth=4) {
  const out=[]; const seen=new Set();
  function walk(dir, depth) {
    let real; try { real=fs.realpathSync(dir); } catch { return; }
    if (seen.has(real) || depth>maxDepth) return; seen.add(real);
    const base=path.basename(dir); if (['node_modules','.git','.cache','.npm'].includes(base)) return;
    const pkgPath=path.join(dir,'package.json');
    if (exists(pkgPath)) {
      try { const pkg=JSON.parse(readText(pkgPath)); const hits=dependencyHits(pkg); if (hits.length) out.push({dir,packageJson:pkgPath,name:pkg.name||path.basename(dir),targets:hits}); } catch {}
    }
    let ents=[]; try { ents=fs.readdirSync(dir,{withFileTypes:true}); } catch { return; }
    for (const e of ents) if (e.isDirectory()) walk(path.join(dir,e.name),depth+1);
  }
  for (const r of roots) if (exists(r)) walk(r,0);
  return out;
}
function scanAll(opts={}) {
  const roots = arr(opts['scan-root']); if (!roots.length) roots.push('/opt','/srv','/app','/var/www',home);
  const processes=scanProcesses();
  return { processes, services:[...new Set(processes.map(p=>p.service).filter(Boolean))], nodeBinaries:scanNodeBinaries(), apps:scanApps(roots, +(opts['max-depth']||4)) };
}

function findTarball(opts) {
  if (opts.package) return path.resolve(opts.package);
  const candidates = [
    path.join(PROJECT_ROOT,'js','senda-argus-hooks-0.3.0.tgz'),
    path.join(PROJECT_ROOT,'senda-argus-hooks-0.3.0.tgz'),
    path.join(SCRIPT_DIR,'senda-argus-hooks-0.3.0.tgz'),
  ];
  for (const c of candidates) if (exists(c)) return c;
  die('Node SDK tarball not found. Use --package /path/to/senda-argus-hooks-0.3.0.tgz');
}
function installRuntime(opts) {
  const runtimeDir=path.resolve(opts['runtime-dir'] || defaults.runtimeDir);
  const tgz=findTarball(opts);
  if (opts['dry-run']) { say(`would install ${tgz} into ${runtimeDir}`); return runtimeDir; }
  fs.mkdirSync(runtimeDir,{recursive:true});
  const r=run('npm',['install','--omit=dev','--omit=peer','--legacy-peer-deps','--no-audit','--no-fund','--prefix',runtimeDir,tgz],{inherit:true});
  if (r.status!==0) die('npm install failed');
  return runtimeDir;
}
function preloadPath(runtimeDir) { return path.join(runtimeDir,'node_modules','@senda','argus-hooks','dist','zerocode','preload.js'); }

function buildConfig(opts) {
  const lines = [
    'SENDA_ARGUS_ENABLED=true',
    `SENDA_ARGUS_EXPORTER=${opts.exporter || opts.exporters || 'jsonl'}`,
    `SENDA_ARGUS_PROJECT=${opts.project || 'default'}`,
    `SENDA_ARGUS_ENVIRONMENT=${opts.environment || 'prod'}`,
    `SENDA_ARGUS_CAPTURE_PROMPT=${opts['capture-prompt'] || 'false'}`,
    `SENDA_ARGUS_CAPTURE_RESPONSE=${opts['capture-response'] || 'false'}`,
    `SENDA_ARGUS_CAPTURE_ARGUMENTS=${opts['capture-arguments'] || 'false'}`,
    `SENDA_ARGUS_CAPTURE_RESULT=${opts['capture-result'] || 'false'}`,
    `SENDA_ARGUS_CAPTURE_HASH=${opts['capture-hash'] || 'true'}`,
    `SENDA_ARGUS_REDACT=${opts.redact || 'true'}`,
    `SENDA_ARGUS_JSONL_PATH=${opts['jsonl-path'] || '/var/log/senda-argus/events.jsonl'}`,
  ];
  if (opts.endpoint) lines.push(`SENDA_ARGUS_ENDPOINT=${opts.endpoint}`);
  let key=opts['api-key']; if (opts['api-key-file']) key=readText(opts['api-key-file']).trim();
  if (key) lines.push(`SENDA_ARGUS_API_KEY=${key}`);
  for (const [arg,env] of [['tenant-id','SENDA_ARGUS_TENANT_ID'],['agent-id','SENDA_ARGUS_AGENT_ID'],['purpose-id','SENDA_ARGUS_PURPOSE_ID']]) if (opts[arg]) lines.push(`${env}=${opts[arg]}`);
  return lines.join('\n')+'\n';
}
function installConfig(opts) {
  const file=path.resolve(opts['config-file'] || defaults.configFile);
  if (opts['dry-run']) { say(`would write ${file}`); return file; }
  writeFile(file,buildConfig(opts),0o600); return file;
}
function existingNodeOptions(unit) {
  const r=run('systemctl',['show',unit,'-p','Environment','--value']);
  if (r.status!==0) return '';
  const m=String(r.stdout).match(/(?:^|\s)NODE_OPTIONS=([^\s"]+|"[^"]*")/);
  return m ? m[1].replace(/^"|"$/g,'') : '';
}
function installSystemdDropin(unit, pre, configFile, opts) {
  if (!/^[A-Za-z0-9_.@:-]+\.service$/.test(unit)) die(`unsafe systemd unit name: ${unit}`);
  const dir = `/etc/systemd/system/${unit}.d`; const file=path.join(dir,'90-senda-argus-node.conf');
  const existing=existingNodeOptions(unit);
  const injection=`--import=${pre}`;
  const nodeOptions=existing.includes(pre) ? existing : `${injection}${existing ? ' '+existing : ''}`;
  const text=`# Managed by Senda-Argus Node Zero-code installer\n[Service]\nEnvironmentFile=-${configFile}\nEnvironment="NODE_OPTIONS=${systemdQuote(nodeOptions)}"\n`;
  if (opts['dry-run']) { say(`would write ${file}`); return file; }
  writeFile(file,text,0o644); return file;
}
function installProfile(pre, configFile, opts) {
  const file=path.resolve(opts['profile-file'] || defaults.profileFile);
  const text=`# Managed by Senda-Argus Node Zero-code installer\nif [ -f ${shellQuote(configFile)} ]; then set -a; . ${shellQuote(configFile)}; set +a; fi\ncase " \${NODE_OPTIONS:-} " in *" --import=${pre} "*) ;; *) export NODE_OPTIONS="--import=${pre}\${NODE_OPTIONS:+ \$NODE_OPTIONS}" ;; esac\n`;
  if (opts['dry-run']) { say(`would write ${file}`); return file; }
  writeFile(file,text,0o644);
  if (!isRoot) {
    const p=path.join(home,'.profile'); const marker='SENDA_ARGUS_NODE_PROFILE'; const current=readText(p);
    if (!current.includes(marker)) fs.appendFileSync(p,`\n# ${marker}\n[ -f ${shellQuote(file)} ] && . ${shellQuote(file)}\n`);
  }
  return file;
}
function readState(file=defaults.stateFile) { try { return JSON.parse(readText(file)); } catch { return {}; } }
function saveState(state,file=defaults.stateFile) { writeFile(file,JSON.stringify(state,null,2)+'\n',0o600); }
function removeManagedProfile(profileFile) {
  try { fs.rmSync(profileFile,{force:true}); } catch {}
  if (!isRoot) {
    const p=path.join(home,'.profile'); let s=readText(p);
    s=s.replace(/\n# SENDA_ARGUS_NODE_PROFILE\n\[ -f .*?\] && \. .*?\n/g,'\n'); try { fs.writeFileSync(p,s); } catch {}
  }
}

function cmdScan(opts) { const s=scanAll(opts); if(opts.json) console.log(JSON.stringify(s,null,2)); else { console.log('Running Node processes:'); for(const p of s.processes) console.log(`  PID ${p.pid} ${p.version?.raw||'?'} ${p.service||'-'} ${p.cwd||'-'} :: ${p.cmdline}`); console.log('\nDetected systemd services:'); for(const u of s.services) console.log(`  ${u}`); console.log('\nNode binaries:'); for(const n of s.nodeBinaries) console.log(`  ${n.exe} ${n.version.raw}${n.version.supported?'':' (zero-code preload unsupported: requires >=18.19)'}`); console.log('\nAgent-like Node projects:'); for(const a of s.apps) console.log(`  ${a.dir} [${a.targets.join(', ')}]`); } }
function cmdInstall(opts) {
  if (opts.all && !opts.yes) die('--all requires --yes');
  if (isRoot===false && arr(opts.service).length) die('systemd system service injection requires root');
  const scan=scanAll(opts); const services=new Set(arr(opts.service));
  for(const p of arr(opts.pid)){ const x=scan.processes.find(v=>String(v.pid)===String(p)); if(!x) die(`PID not found or not Node: ${p}`); if(x.service) services.add(x.service); else say(`PID ${p} is not attached to a detectable systemd service`); }
  if(opts.all) for(const u of scan.services) services.add(u);
  const runtimeDir=installRuntime(opts); const pre=preloadPath(runtimeDir); const configFile=installConfig(opts);
  if(!opts['dry-run'] && !exists(pre)) die(`preload not found after install: ${pre}`);
  const dropins=[]; for(const u of services) dropins.push({unit:u,file:installSystemdDropin(u,pre,configFile,opts)});
  let profileFile=null; if(opts.profile || services.size===0) profileFile=installProfile(pre,configFile,opts);
  if(!opts['dry-run'] && services.size){ if (!hasSystemd()) die('systemd service injection selected but systemd is not active'); run('systemctl',['daemon-reload'],{inherit:true}); if(opts.restart){ for(const u of services) run('systemctl',['restart',u],{inherit:true}); } }
  const state={version:1,installedAt:new Date().toISOString(),runtimeDir,preload:pre,configFile,dropins,profileFile}; if(!opts['dry-run']) saveState(state,path.resolve(opts['state-file']||defaults.stateFile));
  say(`runtime: ${runtimeDir}`); say(`preload: ${pre}`); say(`config: ${configFile}`); if(services.size && !opts.restart) say(`restart required for: ${[...services].join(', ')}`); if(profileFile) say(`profile injection: ${profileFile} (new login/session required)`);
}
function cmdStatus(opts) {
  const sf=path.resolve(opts['state-file']||defaults.stateFile); const st=readState(sf); const status={stateFile:sf,stateExists:exists(sf),runtimeDir:st.runtimeDir||defaults.runtimeDir,preload:st.preload||preloadPath(st.runtimeDir||defaults.runtimeDir),preloadExists:exists(st.preload||preloadPath(st.runtimeDir||defaults.runtimeDir)),configFile:st.configFile||defaults.configFile,configExists:exists(st.configFile||defaults.configFile),dropins:(st.dropins||[]).map(d=>({...d,exists:exists(d.file)})),profileFile:st.profileFile||null,profileExists:st.profileFile?exists(st.profileFile):false,running:scanProcesses()};
  if(opts.json) console.log(JSON.stringify(status,null,2)); else { console.log(JSON.stringify(status,null,2)); }
}
function cmdUninstall(opts) {
  const sf=path.resolve(opts['state-file']||defaults.stateFile); const st=readState(sf);
  for(const d of st.dropins||[]) { if(opts['dry-run']) say(`would remove ${d.file}`); else try { fs.rmSync(d.file,{force:true}); } catch {} }
  if(st.profileFile){ if(opts['dry-run']) say(`would remove ${st.profileFile}`); else removeManagedProfile(st.profileFile); }
  if(opts['remove-config'] && st.configFile){ if(opts['dry-run']) say(`would remove ${st.configFile}`); else try{fs.rmSync(st.configFile,{force:true});}catch{} }
  if(opts['remove-runtime'] && st.runtimeDir){ if(opts['dry-run']) say(`would remove ${st.runtimeDir}`); else try{fs.rmSync(st.runtimeDir,{recursive:true,force:true});}catch{} }
  if(!opts['dry-run']) { try{fs.rmSync(sf,{force:true});}catch{} if(isRoot && hasSystemd()) run('systemctl',['daemon-reload'],{inherit:true}); }
  say('Node Zero-code injection removed. Restart affected services/processes to fully apply.');
}
function help(){ console.log(`Senda-Argus Node Zero-code installer\n\nUsage:\n  node tools/senda_argus_zero_install_node.mjs scan [--json] [--scan-root DIR]\n  sudo node tools/senda_argus_zero_install_node.mjs install --service my-agent.service [options]\n  sudo node tools/senda_argus_zero_install_node.mjs install --pid 1234 [options]\n  sudo node tools/senda_argus_zero_install_node.mjs install --all --yes [options]\n  node tools/senda_argus_zero_install_node.mjs install --profile [options]\n  node tools/senda_argus_zero_install_node.mjs status [--json]\n  sudo node tools/senda_argus_zero_install_node.mjs uninstall [--remove-runtime] [--remove-config]\n\nImportant options:\n  --package FILE          SDK .tgz (auto-detected in bundle)\n  --runtime-dir DIR       central SDK runtime directory\n  --config-file FILE      node.env path\n  --endpoint URL          Senda-Argus endpoint\n  --api-key-file FILE     read API key without exposing it in argv\n  --project NAME          project label\n  --environment NAME      environment label\n  --exporter argus|jsonl|stdout|null\n  --profile               inject into shell profile as well\n  --restart               restart selected systemd services now\n  --dry-run               show changes only\n\nZero-code ESM loader requires Node >=18.19. Node 20/22 LTS is recommended.\n`); }

const opts=parseArgs(process.argv.slice(2)); const cmd=opts._[0]||'help';
if(cmd==='scan') cmdScan(opts); else if(cmd==='install') cmdInstall(opts); else if(cmd==='status') cmdStatus(opts); else if(cmd==='uninstall') cmdUninstall(opts); else help();
