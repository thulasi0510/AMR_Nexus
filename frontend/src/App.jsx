import { useState } from 'react';
import axios from 'axios';
import './styles.css';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:5000';

function InlineMarkdown({ children }) {
  const text = String(children ?? '');
  if (!text) return null;

  // Regex tokenizing:
  // 1. URLs
  // 2. Bold (**text**)
  // 3. Italic (*text*)
  // 4. Inline code (`text`)
  // 5. Numeric citations ([1], [1, 2], [1-3])
  const tokenRegex = /(https?:\/\/[^\s),;]+)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(`[^`]+`)|(\[\d+(?:[-,]\s*\d+)*\])/g;

  const elements = [];
  let lastIndex = 0;
  let match;

  while ((match = tokenRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      elements.push(text.slice(lastIndex, match.index));
    }
    const [fullMatch, urlMatch, boldMatch, italicMatch, codeMatch, citeMatch] = match;

    if (urlMatch) {
      elements.push(
        <a key={elements.length} href={urlMatch} target="_blank" rel="noopener noreferrer" className="inline-link">
          {urlMatch}
        </a>
      );
    } else if (boldMatch) {
      elements.push(
        <strong key={elements.length} className="inline-bold">
          {boldMatch.slice(2, -2)}
        </strong>
      );
    } else if (italicMatch) {
      elements.push(
        <em key={elements.length} className="inline-italic">
          {italicMatch.slice(1, -1)}
        </em>
      );
    } else if (codeMatch) {
      elements.push(
        <code key={elements.length} className="inline-code">
          {codeMatch.slice(1, -1)}
        </code>
      );
    } else if (citeMatch) {
      elements.push(
        <span key={elements.length} className="citation-pill" title={`Citation ${citeMatch}`}>
          {citeMatch}
        </span>
      );
    }
    lastIndex = tokenRegex.lastIndex;
  }

  if (lastIndex < text.length) {
    elements.push(text.slice(lastIndex));
  }

  return (
    <>
      {elements.map((el, i) =>
        typeof el === 'string' ? (
          <span key={i}>{el.replace(/\*\*/g, '')}</span>
        ) : (
          el
        )
      )}
    </>
  );
}

function AnswerContent({ text }) {
  const lines = String(text || '').split('\n');
  const blocks = [];
  let table = [];

  const flushTable = () => {
    if (table.length) {
      blocks.push({ type: 'table', rows: table });
      table = [];
    }
  };

  lines.forEach((line) => {
    const trimmed = line.trim();
    if (trimmed.startsWith('|') && trimmed.endsWith('|')) {
      if (!/^\|\s*:?-{2,}/.test(trimmed)) {
        table.push(
          trimmed
            .split('|')
            .slice(1, -1)
            .map((cell) => cell.trim().replace(/^\*\*(.*)\*\*$/, '$1'))
        );
      }
      return;
    }
    flushTable();
    if (!trimmed) return;
    if (/^#{1,3}\s+/.test(trimmed)) {
      blocks.push({ type: 'heading', text: trimmed.replace(/^#{1,3}\s+/, '') });
    } else if (/^[-*]\s+/.test(trimmed)) {
      blocks.push({ type: 'bullet', text: trimmed.replace(/^[-*]\s+/, '') });
    } else {
      blocks.push({ type: 'paragraph', text: trimmed });
    }
  });
  flushTable();

  return (
    <div className="answer-content">
      {blocks.map((block, index) => {
        if (block.type === 'table') {
          return (
            <div className="table-wrap" key={index}>
              <table className="evidence-table">
                <thead>
                  <tr>
                    {block.rows[0]?.map((cell, i) => (
                      <th key={i}>
                        <InlineMarkdown>{cell}</InlineMarkdown>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {block.rows.slice(1).map((row, r) => (
                    <tr key={r}>
                      {row.map((cell, i) => (
                        <td key={i} className={i === 0 ? 'cell-primary' : ''}>
                          <InlineMarkdown>{cell}</InlineMarkdown>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (block.type === 'heading') {
          return (
            <h3 key={index} className="section-heading">
              <InlineMarkdown>{block.text}</InlineMarkdown>
            </h3>
          );
        }
        if (block.type === 'bullet') {
          return (
            <div className="bullet-row" key={index}>
              <span className="bullet-indicator" aria-hidden="true">•</span>
              <p className="bullet-text">
                <InlineMarkdown>{block.text}</InlineMarkdown>
              </p>
            </div>
          );
        }
        return (
          <p key={index} className="prose-paragraph">
            <InlineMarkdown>{block.text}</InlineMarkdown>
          </p>
        );
      })}
    </div>
  );
}

function SourceStatus({ result }) {
  const supplemental = result?.source_summary?.supplemental;
  if (!supplemental && !result?.retrieval) return null;
  const providers = supplemental?.providers || [];
  const providerStatus = supplemental?.provider_status || {};
  const googleScholar = supplemental?.google_scholar;
  const errors = supplemental?.errors || result?.retrieval?.errors || [];

  return (
    <section className="source-status-bar">
      <span className="source-label">Active Sources</span>
      <span className="source-pill source-primary">PubMed / PMC E-Utilities</span>
      {providers.map((p) => (
        <span key={p} className="source-pill">
          {p} · {providerStatus[p] || 'connected'}
        </span>
      ))}
      {googleScholar?.url && (
        <a
          href={googleScholar.url}
          target="_blank"
          rel="noopener noreferrer"
          className="source-pill"
          style={{ textDecoration: 'none', color: 'inherit' }}
          title="Open Google Scholar exploration query"
        >
          Google Scholar ↗
        </a>
      )}
      <span className="source-pill">CARD AMR Ontology</span>
      <span className="source-pill">NCBI AMRFinderPlus</span>
      {result?.latency?.total_seconds != null && (
        <span className="source-pill latency-pill">
          Response: {result.latency.total_seconds}s
        </span>
      )}
      {errors.length > 0 && (
        <span className="source-warning">Some endpoints timed out; report synthesised with available references.</span>
      )}
    </section>
  );
}

const EXAMPLE_QUERY = "Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?";

export default function App() {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event?.preventDefault?.();
    const submitted = query.trim();
    if (!submitted) return;
    setLoading(true);
    setError('');
    setResult(null);
    try {
      const response = await axios.post(`${API_URL}/query`, { query: submitted }, { timeout: 180000 });
      setResult(response.data);
    } catch (err) {
      setError(
        err.response?.data?.message ||
        err.response?.data?.error ||
        (err.code === 'ECONNABORTED'
          ? 'The research request timed out.'
          : 'The backend service could not be reached. Ensure Flask is running on port 5000.')
      );
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      submit(e);
    }
  }

  // Strip duplicate markdown references from the end of the text answer when structured references are rendered
  const cleanAnswer = (result?.answer || '')
    .replace(/(?:\r?\n){1,3}#{0,3}\s*(?:references(?:\s+found)?|published\s+articles|supporting\s+publications)\b[\s\S]*$/i, '')
    .trim();

  return (
    <div className="app-container">
      <header className="copilot-header">
        <div className="header-brand">
          <div className="brand-badge">
            <span className="brand-dot" /> Agentic AI AMR Research Tool
          </div>
          <h1 className="brand-title">AMR Nexus</h1>
          <p className="brand-subtitle">
            Autonomous multi-agent research engine for investigating antimicrobial resistance determinants, genetic variants, resistance mechanisms, and scholarly literature.
          </p>
        </div>
        <div className="header-meta">
          <div className="status-badge-live">
            <span className="pulse-indicator" /> System Online
          </div>
        </div>
      </header>

      <main className="copilot-workspace">
        <section className="panel query-panel">
          <form onSubmit={submit}>
            <div className="query-header">
              <label htmlFor="query" className="query-label">
                Research Inquiry
              </label>
              <div className="example-query-wrapper">
                <span className="example-query-label">💡 Example Query:</span>
                <button
                  type="button"
                  className="example-query-chip"
                  onClick={() => setQuery(EXAMPLE_QUERY)}
                  title="Click to populate research question"
                >
                  {EXAMPLE_QUERY}
                </button>
              </div>
            </div>

            <textarea
              id="query"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Enter an AMR research query here..."
              rows={4}
              aria-describedby="query-help"
            />

            <div className="query-footer">
              <span id="query-help" className="query-hint">
                <kbd>Ctrl</kbd> + <kbd>Enter</kbd> to run
              </span>
              <button type="submit" className="submit-btn" disabled={loading || !query.trim()}>
                {loading ? (
                  <>
                    <span className="btn-spinner" /> Synthesising Evidence…
                  </>
                ) : (
                  'Run Research'
                )}
              </button>
            </div>
          </form>
        </section>

        {loading && (
          <section className="notice-panel loading" role="status">
            <div className="loading-content">
              <span className="spinner" />
              <div>
                <strong>Autonomous Agents Active</strong>
                <p>Interpreting query intent · Cross-referencing ontologies · Retrieving scientific evidence</p>
              </div>
            </div>
          </section>
        )}

        {error && (
          <section className="notice-panel error" role="alert">
            <div className="error-icon">⚠️</div>
            <div>
              <strong>Research Query Unavailable</strong>
              <p>{error}</p>
              <small>No scientific assertion was inferred from the failed query.</small>
            </div>
          </section>
        )}

        {result?.status === 'pending_selection' && (
          <section className="panel clarification-panel">
            <div className="clarification-header">
              <span className="badge-warning">Ambiguity Detected</span>
              <h2>Entity Disambiguation Required</h2>
            </div>
            <p className="clarification-message">{result.message}</p>
            {(result.candidate_groups || []).map((group) => (
              <div className="candidate-group-card" key={group.mention}>
                <h3>Candidates for &ldquo;{group.mention}&rdquo;</h3>
                <div className="candidate-options">
                  {(group.candidates || []).map((candidate) => (
                    <div className="candidate-item" key={candidate.candidate_label}>
                      <span className="candidate-badge">{candidate.candidate_label}</span>
                      <span className="candidate-name">{candidate.canonical_name}</span>
                      <span className="candidate-source">({candidate.source})</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </section>
        )}

        {result?.answer && (
          <section className="panel answer-panel">
            <div className="answer-header">
              <div>
                <div className="answer-eyebrow">
                  <span className="eyebrow-pill">Verified Evidence Synthesis</span>
                  {result.understanding?.intent?.length > 0 && (
                    <span className="intent-pill">
                      {result.understanding.intent[0].replace(/_/g, ' ')}
                    </span>
                  )}
                </div>
                <h2 className="answer-title">Clinical & Biological Findings</h2>
              </div>
              <div className="answer-status-tags">
                <span className={`status-badge status-${result.status}`}>
                  {result.status === 'success' ? 'Validated Synthesis' : result.status}
                </span>
              </div>
            </div>

            <SourceStatus result={result} />

            <div className="answer-body-container">
              <AnswerContent text={cleanAnswer || result.answer} />
            </div>

            {result.references?.length > 0 && (
              <section className="references-unified">
                <div className="references-header">
                  <h2 className="references-title">References</h2>
                  <span className="references-count-badge">
                    {result.references.length} {result.references.length === 1 ? 'Source' : 'Sources'}
                  </span>
                </div>
                <ol className="references-list">
                  {result.references.map((reference, idx) => {
                    const num = reference.reference_number || idx + 1;
                    const authors = Array.isArray(reference.authors)
                      ? reference.authors.slice(0, 5).join(', ') + (reference.authors.length > 5 ? ' et al.' : '')
                      : reference.authors || '';
                    const directUrl =
                      reference.pubmed_url ||
                      (reference.pmid ? `https://pubmed.ncbi.nlm.nih.gov/${reference.pmid}/` : null) ||
                      reference.doi_url ||
                      (reference.doi ? `https://doi.org/${reference.doi}` : null) ||
                      reference.source_url;

                    return (
                      <li key={`${num}-${reference.pmid || reference.doi || reference.title}`} value={num} className="reference-item">
                        <div className="reference-card">
                          <div className="reference-num-badge">[{num}]</div>
                          <div className="reference-content">
                            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                              <span className="reference-paper-title">{reference.title || 'Untitled publication'}</span>
                              {reference.publication_status === 'Preprint' ? (
                                <span className="source-pill" style={{ background: '#fef3c7', color: '#92400e', borderColor: '#fde68a', fontSize: '0.75rem', padding: '0.1rem 0.45rem' }}>
                                  Preprint: {reference.preprint_server || reference.source || 'bioRxiv'}
                                </span>
                              ) : (
                                <span className="source-pill" style={{ background: '#ecfdf5', color: '#065f46', borderColor: '#a7f3d0', fontSize: '0.75rem', padding: '0.1rem 0.45rem' }}>
                                  Peer-reviewed
                                </span>
                              )}
                            </div>
                            <div className="reference-meta">
                              {authors && (
                                <span className="reference-authors">
                                  {authors}
                                  {reference.year ? ` (${reference.year})` : ''}
                                </span>
                              )}
                              {reference.journal && <span className="reference-journal">{reference.journal}</span>}
                              {reference.pmid && (
                                <span className="reference-pmid">
                                  PMID:{' '}
                                  <a href={`https://pubmed.ncbi.nlm.nih.gov/${reference.pmid}/`} target="_blank" rel="noopener noreferrer">
                                    {reference.pmid}
                                  </a>
                                </span>
                              )}
                              {reference.doi && (
                                <span className="reference-doi">
                                  DOI:{' '}
                                  <a href={`https://doi.org/${reference.doi}`} target="_blank" rel="noopener noreferrer">
                                    {reference.doi}
                                  </a>
                                </span>
                              )}
                            </div>
                            {directUrl && (
                              <div className="reference-action">
                                <a href={directUrl} target="_blank" rel="noopener noreferrer" className="reference-open-link">
                                  Open publication &rarr;
                                </a>
                              </div>
                            )}
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              </section>
            )}
          </section>
        )}

        {!loading && !error && !result && (
          <section className="empty-state-panel">
            <div className="empty-icon">🔬</div>
            <h2>Ready for AMR Research Inquiry</h2>
            <p>Enter any clinical question, resistance gene, mutation, drug pair, or comparative query above to synthesize evidence.</p>
            <div className="empty-suggestions">
              <span>Try asking:</span>
              <button type="button" onClick={() => setQuery("Compare MexAB-OprM and AdeABC efflux pumps")}>
                Compare MexAB-OprM and AdeABC efflux pumps
              </button>
              <button type="button" onClick={() => setQuery("katG S315T")}>
                katG S315T
              </button>
              <button type="button" onClick={() => setQuery("ompK")}>
                ompK
              </button>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
