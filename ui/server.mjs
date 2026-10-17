// Serve only this UI directory. No environment loading, API routes, or proxies.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const publicFiles = new Set([
  '/index.html', '/src/main.js', '/src/scenes.js', '/src/demo-data.js', '/src/styles.css',
]);
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
const port = Number(process.env.PORT || 5173);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid PORT');

const server = createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Referrer-Policy', 'no-referrer');
  response.setHeader('Content-Security-Policy', "default-src 'self'; connect-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
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
});

server.listen(port, '127.0.0.1', () => {
  console.log(`No Free Wins UI preview: http://127.0.0.1:${port}`);
  console.log('Local demo only. Backend and pipeline are disconnected.');
});
