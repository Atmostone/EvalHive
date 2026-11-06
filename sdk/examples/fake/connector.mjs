import { readFileSync } from 'node:fs';
import { serveStdio, ProtocolError } from '@evalhive/harness-sdk';

const manifest = JSON.parse(readFileSync(new URL('./manifest.json', import.meta.url), 'utf8'));
await serveStdio({
  manifest,
  async validate(spec) {
    return {accepted: true, effective_config: {prefix: '', wait: false, fail: false, ...spec.config}};
  },
  async run(context) {
    const config = context.spec.config;
    await context.emit('message.end', {message_id: 'answer', role: 'assistant', content: (config.prefix ?? '') + 'started'});
    if (config.wait) {
      if (!context.signal.aborted) await new Promise(resolve => context.signal.addEventListener('abort', resolve, {once: true}));
      return {status: 'cancelled', summary: 'Run cancelled'};
    }
    if (config.fail) throw new Error('Internal diagnostics must not leak into protocol errors');
    await context.emit('artifact', {path: 'result.txt'});
    return {status: 'success', summary: 'Fake completed'};
  },
  async control(context, name, payload) {
    if (name === 'abort') context.abort.abort();
    else if (name === 'feedback') await context.emit('message.end', {message_id: 'feedback', role: 'user', content: payload.text});
    else throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Unavailable command');
    return {accepted: true};
  },
});
