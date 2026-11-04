import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { once } from 'node:events';
import { copyFileSync, mkdirSync, symlinkSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Config } from '@opencode-ai/sdk/v2';
import { ProtocolError } from '@evalhive/harness-sdk';

export const executable = fileURLToPath(
  new URL('../node_modules/opencode-ai/bin/opencode.exe', import.meta.url),
);

export function isolatedEnv(state: string): NodeJS.ProcessEnv {
  for (const name of ['home', 'config', 'cache', 'data', 'state'])
    mkdirSync(join(state, name), { recursive: true, mode: 0o700 });
  // OpenCode installs @opencode-ai/plugin for config directories unconditionally.
  // Reuse the image's pinned dependency tree and lockfile instead of downloading.
  const config = join(state, 'config', 'opencode');
  mkdirSync(config, { recursive: true, mode: 0o700 });
  const root = fileURLToPath(new URL('../', import.meta.url));
  for (const name of ['package.json', 'package-lock.json'])
    copyFileSync(join(root, name), join(config, name));
  symlinkSync(join(root, 'node_modules'), join(config, 'node_modules'), 'dir');
  return {
    PATH: process.env.PATH,
    HOME: join(state, 'home'),
    TMPDIR: state,
    XDG_CONFIG_HOME: join(state, 'config'),
    XDG_CACHE_HOME: join(state, 'cache'),
    XDG_DATA_HOME: join(state, 'data'),
    XDG_STATE_HOME: join(state, 'state'),
    OPENCODE_DISABLE_PROJECT_CONFIG: '1',
    OPENCODE_DISABLE_MODELS_FETCH: '1',
    OPENCODE_DISABLE_AUTOUPDATE: '1',
    OPENCODE_DISABLE_DEFAULT_PLUGINS: '1',
    OPENCODE_DISABLE_EXTERNAL_SKILLS: '1',
    OPENCODE_DISABLE_CLAUDE_CODE: '1',
    OPENCODE_DISABLE_LSP_DOWNLOAD: '1',
    OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER: '1',
    OPENCODE_DISABLE_TERMINAL_TITLE: '1',
    OPENCODE_DISABLE_EMBEDDED_WEB_UI: '1',
  };
}

export async function startServer(state: string, cwd: string, config: Config) {
  const path = join(state, 'config.json');
  writeFileSync(path, JSON.stringify(config), { mode: 0o600 });
  const env = { ...isolatedEnv(state), OPENCODE_CONFIG: path };
  const proc = spawn(executable, ['serve', '--hostname=127.0.0.1', '--port=0'], {
    cwd,
    env,
    detached: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let diagnostics = '';
  proc.stderr.on('data', (chunk) => {
    diagnostics = (diagnostics + chunk.toString()).slice(-65536);
  });
  const url = await new Promise<string>((resolve, reject) => {
    let output = '';
    const timeout = setTimeout(
      () => reject(new ProtocolError('SERVER_START_TIMEOUT', 'OpenCode did not become ready')),
      30000,
    );
    const done = (error?: Error, address?: string) => {
      clearTimeout(timeout);
      error ? reject(error) : resolve(address!);
    };
    proc.once('error', () =>
      done(new ProtocolError('SERVER_START_FAILED', 'OpenCode server could not start')),
    );
    proc.once('exit', () =>
      done(new ProtocolError('SERVER_EXITED', 'OpenCode server exited during startup')),
    );
    proc.stdout.on('data', (chunk) => {
      output = (output + chunk.toString()).slice(-16384);
      const match = output.match(/opencode server listening on (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) done(undefined, match[1]);
    });
  }).catch(async (error) => {
    await stopServer(proc);
    throw error;
  });
  return { proc, url, diagnostics: () => diagnostics };
}

export async function stopServer(proc: ChildProcessWithoutNullStreams): Promise<void> {
  const exited = once(proc, 'exit').catch(() => {});
  const kill = (signal: NodeJS.Signals) => {
    try {
      if (proc.pid) process.kill(-proc.pid, signal);
    } catch {
      proc.kill(signal);
    }
  };
  if (proc.exitCode === null && proc.signalCode === null) kill('SIGTERM');
  let timeout: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([
    exited,
    new Promise<void>((resolve) => {
      timeout = setTimeout(resolve, 2000);
    }),
  ]);
  if (timeout) clearTimeout(timeout);
  kill('SIGKILL');
  proc.stdin.destroy();
  proc.stdout.destroy();
  proc.stderr.destroy();
}
