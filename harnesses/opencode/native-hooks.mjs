// Connector-owned translation hooks. OpenCode owns the agent and LLM loops.
import { readFileSync, renameSync, writeFileSync } from 'node:fs';

export const EvalHive = async (context, options) => {
  let loaded = false;
  let tools;
  return {
    'chat.params': async (input) => {
      if (!options.maxTurns || ['title', 'summary', 'compaction'].includes(input.agent)) return;
      let response = await context.client.session.messages({
        path: { id: input.sessionID },
        query: { limit: 1 },
        throwOnError: true,
      });
      let assistant = response.data?.findLast((entry) => entry.info.role === 'assistant');
      if (!assistant) {
        response = await context.client.session.messages({
          path: { id: input.sessionID },
          query: { limit: 32 },
          throwOnError: true,
        });
        assistant = response.data?.findLast((entry) => entry.info.role === 'assistant');
      }
      if (!assistant || assistant.info.time.completed !== undefined)
        throw new Error('EvalHive could not identify the active native turn');
      const seen = new Set(JSON.parse(readFileSync(options.turnStatePath, 'utf8')));
      seen.add(`${input.sessionID}/${assistant.info.id}`);
      writeFileSync(options.turnStatePath, JSON.stringify([...seen]), { mode: 0o600 });
      if (seen.size > options.maxTurns) {
        const temporary = options.limitPath + '.tmp';
        writeFileSync(temporary, JSON.stringify({ kind: 'turn_limit' }), { mode: 0o600 });
        renameSync(temporary, options.limitPath);
        throw new Error('EvalHive execution turn limit reached');
      }
    },
    'chat.message': async (_input, output) => {
      if (!loaded) {
        tools = JSON.parse(readFileSync(options.toolPolicyPath, 'utf8'));
        loaded = true;
      }
      // Native prompt.tools also rewrites session permissions. Keep the model's
      // exact tool selection separate so it cannot turn an approval into allow.
      if (tools !== null) output.message.tools = { ...tools };
    },
    'chat.headers': async (input, output) => {
      output.headers['x-evalhive-purpose'] = input.agent;
      output.headers['x-evalhive-session'] = input.sessionID;
    },
  };
};
