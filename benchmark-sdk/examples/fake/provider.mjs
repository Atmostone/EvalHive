import {readFileSync} from 'node:fs';
import {ProtocolError, serveProviderStdio} from '@evalhive/benchmark-sdk';
const manifest = JSON.parse(readFileSync(new URL('./provider-manifest.json', import.meta.url), 'utf8'));
const handles = new Map();
let allocated = 0;
await serveProviderStdio({manifest, async handle(request, context) {
  const {operation, payload, scope, handle} = request;
  if (operation === 'allocate') {
    const id = 'owned-' + ++allocated;
    handles.set(id, {stopped: false});
    await context.emit(scope, 'native', {name: 'resource.allocated', payload: {handle: id}}, 'provider');
    return {handle: id};
  }
  if (!handles.has(handle)) throw new ProtocolError('FOREIGN_HANDLE', 'Resource handle is unavailable', 'configuration');
  if (operation === 'inspect') return {allocated, ...handles.get(handle)};
  if (operation === 'exec') {
    if (payload.fail) throw new Error('private-native-provider-diagnostic');
    if (payload.wait) {
      await new Promise(resolve => {
        if (context.signal.aborted) resolve(); else context.signal.addEventListener('abort', resolve, {once: true});
      });
      throw new ProtocolError('CANCELLED', 'Owned execution cancelled', 'execution');
    }
    return {exit_code: 0, stdout: 'Owned fixture execution'};
  }
  if (operation === 'stop') {handles.get(handle).stopped = true; return {stopped: true};}
  if (operation === 'release') {handles.delete(handle); return {released: true};}
  throw new ProtocolError('UNSUPPORTED_CAPABILITY', 'Provider operation is unavailable', 'unsupported');
}});
