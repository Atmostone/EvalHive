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
      if (options.externalSteps) {
        const response = await context.client.session.messages({path: {id: input.sessionID}, query: {limit: 32}, throwOnError: true});
        const assistant = response.data?.findLast(entry => entry.info.role === 'assistant' && entry.info.time.completed === undefined);
        if (!assistant) throw new Error('EvalHive could not correlate the native model step');
        output.headers['x-evalhive-message'] = assistant.info.id;
      }
    },
    'tool.execute.before': async (input, output) => {
      if (options.externalSteps) {
        // Added after native argument validation. The loopback MCP gateway removes
        // this transport field before checking the original model arguments.
        output.args.__evalhive_native_call_id = input.callID;
      }
    },
    'tool.execute.after': async (input) => {
      if (options.externalSteps) delete input.args.__evalhive_native_call_id;
    },
    'experimental.chat.messages.transform': async (_input, output) => {
      if (options.externalSteps) for (const message of output.messages)
        for (const part of message.parts) if (part.type === 'tool' && part.state.input)
          delete part.state.input.__evalhive_native_call_id;
    },
  };
};
