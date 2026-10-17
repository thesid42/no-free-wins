// Serve only this UI directory plus the read-only replay API (ui/bridge). No .env loading, no proxies,
// no run launching. Binds to 127.0.0.1 only.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApi } from './bridge/api.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const publicFiles = new Set([
  '/index.html', '/src/main.js', '/src/scenes.js', '/src/demo-data.js', '/src/styles.css',
  '/src/adapter.js',
  '/src/presentation.js',
  '/src/story.js',
]);
const repoRoot = resolve(root, '..');
const api = createApi({ repoRoot, runsRoot: resolve(repoRoot, 'artifacts', 'runs') });
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
const port = Number(process.env.PORT || 5173);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid PORT');

const server = createServer(async (request, response) => {
  try {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
    if (await api.handle(request, response)) return; // /api/*: GET/HEAD, plus POST for the chat route only
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.writeHead(405, { Allow: 'GET, HEAD' }).end('Method not allowed');
      return;
    }
    try {
      const pathname = new URL(request.url, 'http://localhost').pathname;
      const route = pathname === '/' ? '/index.html' : pathname;
      const target = resolve(root, '.' + route);
      if (!publicFiles.has(route) || !target.startsWith(root + sep)) {
        response.writeHead(404).end('Not found');
        return;
      }
      const bytes = await readFile(target);
      response.writeHead(200, { 'Content-Type': `${types[extname(target)]}; charset=utf-8` });
      response.end(request.method === 'HEAD' ? undefined : bytes);
    } catch {
      response.writeHead(404).end('Not found');
    }
  } catch (error) { // backstop: a request must never take the server down
    console.error('request failed:', error?.code ?? error?.name ?? 'error'); // no URL, headers or message text
    if (!response.headersSent) response.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' }).end('Internal error');
    else if (!response.writableEnded) response.end();
  }
});

server.listen(port, '127.0.0.1', () => {
  console.log(`No Free Wins UI preview: http://127.0.0.1:${port}`);
  console.log('Read-only replay of recorded runs in artifacts/runs. No run launching, no model calls.');
});
