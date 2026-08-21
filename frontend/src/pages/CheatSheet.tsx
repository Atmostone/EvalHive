import { BookMarked } from 'lucide-react'

type Term = { name: string; desc: string }
type Group = { title: string; blurb?: string; terms: Term[] }

// Plain-language reference for the statistical terms that show up in the reports.
const STATS: Term[] = [
  {
    name: "Welch's t-test (Welch p)",
    desc: "Compares the AVERAGE score of two groups (e.g. two configs), allowing for different spread. The p-value is the chance the difference is just noise — below 0.05 (★) means it is statistically significant (a real difference, not luck).",
  },
  {
    name: 'Mann-Whitney U (Mann-Whitney p)',
    desc: "A rank-based, distribution-free alternative to the t-test — it asks whether one group tends to rank higher, without assuming a bell curve. Safer on small or skewed samples; shown next to Welch as a cross-check. If both agree, trust it more.",
  },
  {
    name: 'Paired test (paired by case)',
    desc: "The experiment runs the SAME cases through every configuration, so the honest comparison is within each case: subtract, then ask whether the differences lean one way. Everything the two configs share — a case being hard, a rubric dimension being lenient — cancels. An unpaired test throws that away and is often blind to a real, consistent improvement because the gap between an easy case and a hard one is bigger than the gap between the configs.",
  },
  {
    name: 'Design: paired vs unpaired',
    desc: "Paired = the two configs finished enough of the same cases (at least 3) and were compared case by case; 'n=4' is the number of shared cases, NOT the number of runs — repeated runs of one case are averaged into it, because ten runs of one case are still one case. Unpaired = too few shared cases, so the comparison falls back to Welch, which is weaker.",
  },
  {
    name: 'p-value · q-value (★)',
    desc: "p is the chance THIS difference is noise. But the report runs dozens of comparisons at once, and at p < 0.05 roughly one in twenty comes up green with nothing behind it — so on 48 tests you expect about two fake stars for free. q is p adjusted for how many tests were run (Benjamini-Hochberg), and ★ follows q, not p. A row marked 'was p<.05' passed on its own and did not survive the company it was in.",
  },
  {
    name: 'Test families (confirmatory / exploratory)',
    desc: "The two headline metrics (Overall quality, Trajectory) are what the experiment was built to compare; the per-dimension rows are a screen over whatever the rubric happened to contain. They are corrected separately, so a real finding on the headline is not punished for forty curiosities — and each family prints how many tests it ran.",
  },
  {
    name: 'Effect size · Hedges\' g · Cohen\'s d_z',
    desc: "The size of a difference, in units of its own spread — because 'significant' only says a difference is there, not whether it matters. d_z is used on paired rows (standardised by the spread of the within-case differences), g on unpaired ones (standardised by the spread within the groups). They answer different questions and must never be compared to each other, which is why the report names which one it is showing.",
  },
  {
    name: 'TOST · equivalence (≈)',
    desc: "'Not significant' means 'we could not tell', which is NOT 'they are the same' — on four cases you can barely tell anything. TOST asks the opposite question: is the difference small enough to be inside a margin we set in advance (0.5 judge points by default)? ≈ equivalent = yes, and that is a claim that could have been wrong. ? inconclusive = the data cannot support either verdict.",
  },
  {
    name: 'MDE · required n (power)',
    desc: "What the design could have seen. MDE is the smallest difference this many cases could have detected; required n is how many cases the difference actually observed would have needed. Together they separate 'we looked and there is nothing' from 'this experiment was never big enough to find it' — which look identical on the page without them.",
  },
  {
    name: 'Wilson interval',
    desc: "A confidence interval for a percentage (pass rate, agreement, the 2×2 cells). The textbook formula misbehaves badly on small counts — it can run below 0% or above 100%, and on a zero cell it reports [0%, 0%], claiming certainty from no evidence at all. Wilson does neither.",
  },
  {
    name: 'Estimand · survivor conditioning',
    desc: "Which population the numbers actually describe. Quality and trajectory scores exist only for runs that FINISHED, so a configuration that crashes more often is scored on its luckier subset and can look better than it is; the same applies per case — a case one config failed is missing from the pairing. The report states this rather than assuming you knew.",
  },
  {
    name: 'Pearson correlation',
    desc: "How LINEARLY two sets of scores move together (−1 to 1; 1 = perfect line, 0 = unrelated). Used for judge-vs-human: high Pearson means when the human scores high, the judge does too.",
  },
  {
    name: 'Spearman correlation',
    desc: "Like Pearson but on RANKS — does the judge order the runs the same way the human does? Robust to outliers and to relationships that are monotonic but not straight-line.",
  },
  {
    name: "Cohen's κ (kappa)",
    desc: "Agreement on a verdict, CORRECTED for the agreement you'd expect by chance (0 = chance level, 1 = perfect). Stricter than raw % agreement. A judge can correlate with a human (high Pearson) yet have low κ if it is systematically offset — that gap is the point of κ. We treat κ ≥ 0.6 as reliable.",
  },
  {
    name: 'Bias',
    desc: "Judge mean − human mean. 0 = unbiased; positive = the judge over-credits (scores higher than the human); negative = under-credits. A big bias with high correlation means 'tracks the human but on a shifted scale'.",
  },
  {
    name: 'Reliability bands',
    desc: "How far a judge axis can be trusted, from real calibration: reliable_absolute (κ ≥ 0.6) and moderate_agreement (0.4–0.6) may carry numeric aggregates; rank_only (κ below the bar but rank ρ ≥ 0.5 — a scale-shifted judge) may carry a rank test on its own scores and never a mean, a Pareto point or a leaderboard place; insufficient, unreliable and not_calibrated carry nothing. The report is computed twice: raw keeps every axis, the Trusted view keeps only what cleared the gate and states what it dropped.",
  },
  {
    name: 'ECE / Brier (confidence calibration)',
    desc: "How well a model's stated confidence matches its actual hit rate. ECE (Expected Calibration Error) is the average gap between confidence and accuracy across buckets (0 = perfectly calibrated); Brier is the mean squared error of the probability (lower = better). They feed the reliability diagram.",
  },
  {
    name: 'Pareto frontier',
    desc: "The set of configs you can't improve on one axis (e.g. quality) without giving up another (cost/time). On the frontier = an optimal trade-off; a config behind it is beaten by some config on every axis at once. Used in the quality × cost × time analysis.",
  },
  {
    name: 'Bradley-Terry / Elo',
    desc: "Rating models built from pairwise 'which is better' matches. Each player (model/config) gets a strength number; the gap predicts win probability. Bradley-Terry fits all matches at once (MLE); Elo updates iteratively. They turn pairwise comparisons into a leaderboard.",
  },
  {
    name: 'Bootstrap CI (confidence interval)',
    desc: "Estimates the uncertainty of a rating/metric by re-sampling the data with replacement many times and recomputing each time; the spread gives the interval (e.g. a rating's 95% CI). Shows how stable a rating is on a small sample.",
  },
  {
    name: 'Not measurable (vs failed)',
    desc: "A gate failure the deliverable did not earn. Every judge call asks the model to answer through a named tool; some providers treat that as advice and reply in plain prose instead. The dimension then cannot be scored at all, and a critical one fails closed — we will not certify what we did not measure. The verdict is the same either way, so the report counts these separately: a config with a low pass rate for this reason is being under-measured, not out-performed.",
  },
  {
    name: 'Thinking vs writing (reasoning tokens)',
    desc: "A reasoning model answers in two streams: private deliberation and the reply. The deliberation is billed inside the output token count, so before it was separated a model that thought hard looked expensive and shallow at once — both halves of that impression being artefacts of one number. The Thinking column is the share of output tokens spent deliberating; a dash means the model or the provider reported no split, which is not the same as zero.",
  },
  {
    name: 'Reasoning shown (process judge)',
    desc: "Whether the process judge was allowed to read the model's own deliberation. It matters because two of the axes — error recovery and goal alignment — are questions about intent, which the judge otherwise infers from tool calls and a final answer. The counter-argument is real too: private reasoning is not behaviour, and grading it rewards models that narrate well. So it is a per-experiment setting, and the choice is recorded with the score — a run judged with reasoning visible is not comparable to one judged without.",
  },
  {
    name: 'Orchestrator cost',
    desc: "What the platform spent deciding about a run — choosing a template, deciding whether to split the task, reviewing the result — as opposed to what the agent spent doing it. Reported as its own column and never folded into the agent's own tokens, because the agent's token count is what efficiency comparisons between models are built on.",
  },
]

// Every evaluator / metric in the platform, described in plain language (no codes).
const GROUPS: Group[] = [
  {
    title: 'Outcome — was the answer right?',
    terms: [
      { name: 'The outcome judge', desc: "An LLM judge that scores the agent's final deliverable against a multi-dimension rubric, one model call per dimension." },
      { name: 'The executable checker', desc: 'A Toolathlon-style script that runs in a container and produces an objective pass/fail — the outcome ground truth on verifiable tasks.' },
      { name: 'The reference-answer check', desc: 'Scores the result against a stored gold reference answer via exact, fuzzy, or semantic match.' },
      { name: 'The objective code check', desc: 'A deterministic, non-LLM check that runs static code analysis (ruff/mypy) on code deliverables.' },
    ],
  },
  {
    title: 'Process — how did the agent work?',
    terms: [
      { name: 'The trace cleaner', desc: 'A deterministic, LLM-free step that compacts a raw 20–30K-token trace into a compact, judge-ready trajectory.' },
      { name: 'The trajectory judge', desc: 'An LLM judge that scores HOW the agent worked across six axes: efficiency, tool selection, parameter quality, error recovery, goal alignment, loop detection.' },
      { name: 'The evidence-bank trace judge', desc: 'A trajectory-judge variant that walks the cleaned trace step by step against an accumulating bank of established facts, adding a groundedness signal.' },
      { name: 'The trajectory match', desc: "A deterministic, LLM-free comparison of the agent's tool-call sequence against a canonical/gold trajectory (exact, edit-distance, and graph metrics)." },
      { name: 'The deterministic loop counter', desc: 'An LLM-free detector that counts repeated tool-calls over the full untrimmed trace — a structural lower bound on looping that replaces the unreliable judge loop signal.' },
    ],
  },
  {
    title: 'Robustness',
    terms: [
      { name: 'The variance harness', desc: 'Re-runs one scenario N times under a cost cap and measures how much the result and trajectory vary across repeats.' },
      { name: 'The perturbation harness', desc: 'Tests robustness by replaying a task under input transforms — paraphrase, noise, reorder, prompt injection.' },
      { name: 'The capability glass-box test', desc: 'Checks whether the agent genuinely USED the required tool/capability rather than answering from memory or failing.' },
    ],
  },
  {
    title: 'Failure & facts',
    terms: [
      { name: 'The failure-mode classifier', desc: 'An LLM classifier that labels a failed run with its failure types: tool confusion, parameter-blind, loop, premature stop, hallucinated tool result, ignored error.' },
      { name: 'The hallucination check', desc: "A hybrid fact-check of the deliverable's URLs, APIs, numbers, and citations against what the trace actually established." },
      { name: 'The model-confidence check', desc: 'Asks the model how confident it is and compares that to actual correctness (calibration error: ECE, Brier, reliability diagram).' },
    ],
  },
  {
    title: 'Calibration & trust',
    terms: [
      { name: 'The human ratings', desc: "A person's per-dimension ratings plus an approve/reject verdict on a run — the ground-truth oracle used to calibrate the judges." },
      { name: 'Judge-vs-human calibration', desc: 'Compares judge scores to human ratings per axis (Pearson, Spearman, Cohen’s κ); an axis counts as reliable at κ ≥ 0.6.' },
      { name: 'Checker-vs-human agreement', desc: 'Pairs the executable checker’s pass/fail with the human approve/reject verdict — showing that even the "ground-truth" checker disagrees with the human gold sometimes (over-credits and false-negatives).' },
      { name: 'The per-axis reliability gate', desc: 'Badges each trajectory axis by how far the judge can be trusted and quarantines axes below the bar, so an unreliable axis can’t silently imply a process win.' },
      { name: 'The judge-bias controls', desc: 'Detect and mitigate judge biases such as position, verbosity, self-preference, and scale compression.' },
    ],
  },
  {
    title: 'Effort, ranking & reporting',
    terms: [
      { name: 'Confound-controlled effort', desc: 'Measures effort as LLM tokens (not wall-clock, which is polluted by provider throttling and waits), normalized by per-case difficulty.' },
      { name: 'The ranking leaderboard', desc: 'Turns pairwise match outcomes into a Bradley-Terry / Elo leaderboard of models or configs.' },
      { name: 'The pairwise A/B judge', desc: 'An LLM head-to-head comparison of two results with position-bias control, feeding the leaderboard.' },
      { name: 'The longitudinal trend', desc: 'A report view of quality and cost trends across repeated runs over time.' },
    ],
  },
  {
    title: 'Infrastructure',
    terms: [
      { name: 'The run-data lake', desc: 'The immutable store that captures every agent run (a summary row in Postgres plus the full blob in object storage) and feeds all the evaluators.' },
      { name: 'The reproducibility snapshot', desc: 'Captures a run’s exact state (model, temperature, seed, memory, tools, input) with a fingerprint so the run can be replayed.' },
      { name: 'Judge-only evaluation mode', desc: 'Skips the executable checker so the outcome judge becomes the evaluator where there is no objective oracle.' },
    ],
  },
]

function TermRow({ t, n }: { t: Term; n?: number }) {
  return (
    <div className="py-2 border-b last:border-0">
      <div className="text-sm font-medium text-gray-900">
        {n != null ? <span className="text-gray-400">{n}. </span> : null}
        {t.name}
      </div>
      <div className="text-sm text-gray-600 mt-0.5">{t.desc}</div>
    </div>
  )
}

export default function CheatSheet() {
  return (
    <div className="p-6 max-w-4xl space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
          <BookMarked className="h-6 w-6" />
          Cheat sheet
        </h1>
        <p className="text-sm text-gray-500 mt-1">
          Plain-language reference for the statistics and the evaluators used across the reports — no internal codes.
        </p>
      </div>

      <section>
        <h2 className="text-lg font-semibold text-gray-900 mb-1">Statistics</h2>
        <p className="text-xs text-gray-500 mb-2">The terms behind the significance and calibration tables.</p>
        <div className="bg-white border rounded-lg p-4">
          {STATS.map((t) => <TermRow key={t.name} t={t} />)}
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold text-gray-900 mb-1">Evaluators &amp; metrics</h2>
        <p className="text-xs text-gray-500 mb-3">
          Every signal the platform can compute on a run — 27 modules across 7 families, grouped by what they measure.
        </p>
        <div className="space-y-5">
          {GROUPS.map((g, gi) => {
            const offset = GROUPS.slice(0, gi).reduce((s, gg) => s + gg.terms.length, 0)
            return (
              <div key={g.title}>
                <h3 className="text-sm font-semibold text-gray-800 mb-1">{g.title}</h3>
                <div className="bg-white border rounded-lg px-4">
                  {g.terms.map((t, i) => (
                    <TermRow key={t.name} t={t} n={offset + i + 1} />
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}
