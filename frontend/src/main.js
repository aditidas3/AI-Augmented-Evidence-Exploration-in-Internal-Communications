import React from '../vendor/react.bundle.mjs';
import { createRoot } from '../vendor/react-dom-client.bundle.mjs';
import { html } from './html.js';
import { App } from './app.js';

function renderBootError(error) {
  const root = document.getElementById('root');
  if (!root) return;
  root.innerHTML = `<pre style="padding:16px;color:#991b1b;white-space:pre-wrap">${String(error?.stack || error?.message || error)}</pre>`;
}

window.addEventListener('error', (event) => renderBootError(event.error || event.message));
window.addEventListener('unhandledrejection', (event) => renderBootError(event.reason));

createRoot(document.getElementById('root')).render(html`<${App} />`);
