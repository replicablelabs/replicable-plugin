export const meta = {
  name: 'map-codebase',
  description: 'Fan out one subagent per module (seeded with code-graph facts), adversarially check each summary, then synthesise ARCHITECTURE.md',
  whenToUse: 'Run via the /replicable-harness:map-codebase skill, which supplies args.modules from the code-graph overview.',
  phases: [
    { title: 'Summarise', detail: 'one reader per module, grounded in code-graph symbol/impact' },
    { title: 'Check', detail: 'a skeptic verifies each summary against the code' },
    { title: 'Synthesise', detail: 'one writer produces the architecture document' },
  ],
}

// args: { modules: [{ file, module, imports, hot_symbols }], out: "docs/ARCHITECTURE.md" }
const modules = (args && args.modules) || []
const out = (args && args.out) || 'docs/ARCHITECTURE.md'
if (!modules.length) {
  throw new Error('args.modules is empty: run the map-codebase skill, which builds it from the code-graph overview.')
}

const SUMMARY = {
  type: 'object',
  properties: {
    file: { type: 'string' },
    purpose: { type: 'string', description: 'one or two sentences' },
    key_symbols: {
      type: 'array',
      items: {
        type: 'object',
        properties: { id: { type: 'string' }, location: { type: 'string' }, role: { type: 'string' } },
        required: ['id', 'location', 'role'],
      },
    },
    depends_on: { type: 'array', items: { type: 'string' } },
    used_by: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' }, description: 'fragile spots, missing tests, hidden coupling' },
  },
  required: ['file', 'purpose', 'key_symbols', 'depends_on', 'used_by', 'risks'],
}

const VERDICT = {
  type: 'object',
  properties: {
    accurate: { type: 'boolean' },
    corrections: { type: 'array', items: { type: 'string' } },
    corrected: SUMMARY,
  },
  required: ['accurate', 'corrections', 'corrected'],
}

// Pipeline: a module's check starts as soon as its own summary lands (no barrier).
const checked = await pipeline(
  modules,
  (m) => agent(
    `Summarise the module ${m.file} (${m.module}) for an architecture document.\n` +
    `Known from the code graph: imports ${JSON.stringify(m.imports || [])}; hot symbols ${JSON.stringify(m.hot_symbols || [])}.\n` +
    `Use the code-graph MCP tools (symbol, callers, impact) for structure, and read only the line ranges they point to. ` +
    `Locations must be path:line.`,
    { label: `summarise:${m.file}`, phase: 'Summarise', schema: SUMMARY, effort: 'low' },
  ),
  (summary, m) => summary && agent(
    `A colleague wrote this summary of ${m.file}. Try to prove it wrong: check every claimed symbol, ` +
    `location and dependency against the code and the code-graph tools. Default to accurate=false if a claim cannot be verified.\n\n` +
    JSON.stringify(summary, null, 1),
    { label: `check:${m.file}`, phase: 'Check', schema: VERDICT },
  ).then((v) => (v ? { ...v.corrected, corrections: v.corrections } : summary)),
)

const summaries = checked.filter(Boolean)
const dropped = modules.length - summaries.length
if (dropped) log(`${dropped} module(s) produced no summary and are missing from the document`)
log(`${summaries.length} module summaries verified; ${summaries.filter((s) => (s.corrections || []).length).length} needed corrections`)

phase('Synthesise')
// Barrier is justified here: the document needs every module at once.
const doc = await agent(
  `Write ${out}: an architecture document for this repository from these verified module summaries.\n` +
  `Sections: Overview (what the system does, in 5 lines), Module map (a mermaid flowchart; you may call the code-graph mermaid tool), ` +
  `Module by module, Cross-cutting risks, Where to start reading. Keep it under 250 lines and keep every path:line reference.\n` +
  (dropped ? `Note that ${dropped} module(s) could not be summarised.\n` : '') +
  `Write the file, then return its table of contents.\n\n` + JSON.stringify(summaries, null, 1),
  { label: 'write-architecture', phase: 'Synthesise' },
)

return { out, modules: summaries.length, dropped, toc: doc }
