/**
 * G 网格模块 · 检查用静态服务器（零依赖）
 *
 * 用途：给 frontend-self-check / frontend-interaction-check / drag-check 提供
 * 一个确定端口的静态服务（默认 8930，契约第 6 节）。
 *
 * 规则：
 *   - 只读地伺服仓库根目录，不写任何文件。
 *   - 端口被占用时**直接报错退出**，绝不停止他人进程。
 *
 * 用法：
 *   node frontend/modules/report-grid/checks/static-server.mjs [--port 8930] [--root <dir>]
 */

import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
/** 仓库根目录：checks → report-grid → modules → frontend → 根 */
const defaultRoot = path.resolve(here, '..', '..', '..', '..');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.htm': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
};

function parseArgs(argv) {
  const out = { port: 8930, root: defaultRoot };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--port') out.port = Number(argv[++i]);
    else if (arg === '--root') out.root = path.resolve(argv[++i]);
  }
  return out;
}

async function statSafe(p) {
  try {
    return await stat(p);
  } catch {
    return null;
  }
}

const args = parseArgs(process.argv.slice(2));
const root = args.root;

const server = createServer(async (req, res) => {
  try {
    const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
    const rel = urlPath.replace(/^\/+/, '');
    let filePath = path.resolve(root, rel);
    if (!filePath.startsWith(root)) {
      res.writeHead(403).end('forbidden');
      return;
    }
    let info = await statSafe(filePath);
    if (!info || info.isDirectory()) {
      const idx = path.join(filePath, 'index.html');
      if (existsSync(idx)) {
        filePath = idx;
        info = await statSafe(filePath);
      } else {
        info = null;
      }
    }
    if (!info) {
      res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' }).end('404 Not Found');
      return;
    }
    const body = await readFile(filePath);
    res.writeHead(200, {
      'content-type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
      'cache-control': 'no-store',
    });
    res.end(body);
  } catch (error) {
    res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' }).end(String(error && error.message));
  }
});

server.on('error', (error) => {
  if (error && error.code === 'EADDRINUSE') {
    console.error(`端口 ${args.port} 已被占用：${error.message}`);
    console.error('请换端口运行，不要停止他人进程。');
    process.exit(3);
  }
  throw error;
});

server.listen(args.port, '127.0.0.1', () => {
  console.log(`静态服务已启动: http://127.0.0.1:${args.port}/  根目录: ${root}`);
});
