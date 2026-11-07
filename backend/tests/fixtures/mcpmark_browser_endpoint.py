"""Deterministic browser responses extract public DOM data, never checker gold."""

import json
import re

from tests.fixtures.mcpmark_postgres_endpoint import MCPMarkPostgresEndpoint

EXTRACT_TABLE = """async () => {
  const rows = [];
  const cells = selector => [...document.querySelectorAll(selector)].map(
    row => [...row.querySelectorAll('th,td')].map((cell, index) =>
      index ? cell.innerText.trim().replaceAll(',', '') : cell.innerText.trim())
  );
  const headers = cells('thead tr')[0];
  if (!headers || headers.length !== 5) throw new Error('Expected public table');
  for (let page = 0; page < 20; page++) {
    const current = cells('tbody tr');
    if (!current.length) throw new Error('Public table has no rows');
    rows.push(...current);
    const next = document.querySelector('[data-testid="next-page-button"]');
    if (!next || next.disabled) {
      const csv = [headers, ...rows].map(row => row.map(
        value => /[",\\n]/.test(value) ? '"' + value.replaceAll('"', '""') + '"' : value
      ).join(',')).join('\\n');
      return 'EVALHIVE_CSV_BEGIN\\n' + csv + '\\nEVALHIVE_CSV_END';
    }
    const before = JSON.stringify(current);
    next.click();
    let changed = false;
    for (let wait = 0; wait < 100; wait++) {
      await new Promise(resolve => setTimeout(resolve, 25));
      if (JSON.stringify(cells('tbody tr')) !== before) { changed = true; break; }
    }
    if (!changed) throw new Error('Public table pagination did not advance');
  }
  throw new Error('Public table exceeds bounded engineering fixture');
}"""


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


class MCPMarkBrowserEndpoint(MCPMarkPostgresEndpoint):
    model = "fixture-mcpmark-browser"

    def message(self, body, step):
        tools = [tool["function"]["name"] for tool in body["tools"]]
        assert not {"bash", "read", "write", "edit"} & set(tools)
        if step >= 2:
            for text in strings(body["messages"]):
                result = re.search(r"### Result\s*\n([^\n]+)", text)
                if result:
                    try:
                        text = json.loads(result[1])
                    except (ValueError, TypeError):
                        continue
                if isinstance(text, str) and "EVALHIVE_CSV_BEGIN\n" in text:
                    csv = text.split("EVALHIVE_CSV_BEGIN\n", 1)[1].split("\nEVALHIVE_CSV_END", 1)[0]
                    assert csv.startswith("Title,") and csv.count("\n") >= 10
                    return {"role": "assistant", "content": csv}
            raise AssertionError("Confirmed browser result was not delivered to the model")
        suffix, arguments = (
            ("browser_navigate", {"url": "https://eval-web.mcpmark.ai/extraction"})
            if step == 0
            else ("browser_evaluate", {"function": EXTRACT_TABLE})
        )
        name = next(name for name in tools if name.endswith(suffix))
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "native-browser-" + str(step),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
