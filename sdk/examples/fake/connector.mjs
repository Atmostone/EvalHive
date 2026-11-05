import { readFileSync } from 'node:fs';
import { serveStdio, ProtocolError } from '@evalhive/harness-sdk';

const manifest = JSON.parse(readFileSync(new URL('./manifest.json', import.meta.url), 'utf8'));
await serveStdio({
  manifest,
  async validate(spec) {
    return {accepted: true, effective_config: {prefix: '', wait: false, fail: false, ...spec.config}};
  },
  async run(context) {
    if (context.conversation) {
      const conversation = context.conversation;
      let summary = 'Conversation closed', incoming;
      while ((incoming = await conversation.nextInput()) !== null) {
        const content = incoming.messages.map(message => message.content).join('\n');
        if (content === 'tools') {
          const results = await conversation.requestTools('message-' + incoming.turn_id, 'batch-' + incoming.turn_id, [
            {call_id: 'first', name: 'echo', arguments: {text: 'one'}},
            {call_id: 'second', name: 'echo', arguments: {text: 'two'}},
          ]);
          summary = JSON.stringify(results.map(result => result.content));
        } else if (content === 'wait') {
          if (!context.signal.aborted) await new Promise(resolve => context.signal.addEventListener('abort', resolve, {once: true}));
          continue;
        } else summary = 'Echo: ' + content;
        await conversation.completeTurn('final-' + incoming.turn_id, summary);
      }
      return {status: 'success', summary};
    }
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
