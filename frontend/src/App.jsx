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

const LITERATURE_SOURCES = [
  {
    id: 'pubmed',
    name: 'PubMed',
    url: 'https://pubmed.ncbi.nlm.nih.gov/',
    description: '36M+ biomedical citations & MEDLINE abstracts from the National Library of Medicine (NCBI). Primary peer-reviewed medical index.',
    records: '36M+ citations',
    logo: (
      <div className="pubmed-brand">
        <span className="pubmed-pub">Pub</span>
        <span className="pubmed-med">Med</span>
      </div>
    ),
  },
  {
    id: 'europepmc',
    name: 'Europe PMC',
    url: 'https://europepmc.org/',
    description: '44M+ life science publications, PMC open access full texts, preprints, patents, and biological database cross-references.',
    records: '44M+ records',
    logo: (
      <svg viewBox="0 0 36 36" className="source-svg-logo" aria-label="Europe PMC">
        <circle cx="14" cy="14" r="8" fill="#0077c8" fillOpacity="0.9" />
        <circle cx="22" cy="14" r="8" fill="#dc2626" fillOpacity="0.9" />
        <circle cx="14" cy="22" r="7.5" fill="#06b6d4" fillOpacity="0.85" />
        <circle cx="22" cy="22" r="7.5" fill="#f59e0b" fillOpacity="0.85" />
      </svg>
    ),
  },
  {
    id: 'semanticscholar',
    name: 'Semantic Scholar',
    url: 'https://www.semanticscholar.org/',
    description: '200M+ AI-indexed scientific papers with deep citation graphs and influential citations from the Allen Institute for AI.',
    records: '200M+ papers',
    logo: (
      <svg viewBox="0 0 36 36" className="source-svg-logo" aria-label="Semantic Scholar">
        <path d="M7 9 L18 15 L18 29 L7 23 Z" fill="#1e3a8a" />
        <path d="M18 15 L29 9 L29 23 L18 29 Z" fill="#2563eb" />
        <path d="M11 17 L17 23 L31 10" fill="none" stroke="#ea580c" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
  },
  {
    id: 'crossref',
    name: 'Crossref',
    url: 'https://search.crossref.org/',
    description: '150M+ scholarly digital object identifiers (DOIs) and verified publisher metadata across global journals and book chapters.',
    records: '150M+ DOIs',
    logo: (
      <svg viewBox="0 0 36 36" className="source-svg-logo" aria-label="Crossref">
        <polygon points="18,5 28,11 18,17 8,11" fill="#f59e0b" />
        <polygon points="8,13 18,19 18,31 8,25" fill="#ef4444" />
        <polygon points="18,19 28,13 28,25 18,31" fill="#2563eb" />
        <polygon points="13,16 23,10 23,16 13,22" fill="#10b981" fillOpacity="0.95" />
      </svg>
    ),
  },
  {
    id: 'openalex',
    name: 'OpenAlex',
    url: 'https://openalex.org/',
    description: '250M+ fully open universal scholarly knowledge graph connecting papers, researchers, institutions, and topics.',
    records: '250M+ works',
    logo: (
      <svg viewBox="0 0 36 36" className="source-svg-logo" aria-label="OpenAlex">
        <polygon points="18,4 30,11 18,18 6,11" fill="#7c3aed" />
        <polygon points="6,11 18,18 18,32 6,25" fill="#db2777" />
        <polygon points="18,18 30,11 30,25 18,32" fill="#f59e0b" />
        <polygon points="18,10 24,14 18,18 12,14" fill="#4f46e5" />
        <circle cx="18" cy="18" r="4" fill="#ffffff" />
        <circle cx="18" cy="18" r="2.2" fill="#6d28d9" />
      </svg>
    ),
  },
  {
    id: 'googlescholar',
    name: 'Google Scholar',
    url: 'https://scholar.google.com/',
    description: 'Broadly searches across disciplines and sources: articles, theses, books, abstracts and court opinions.',
    records: 'Comprehensive Index',
    logo: (
      <svg viewBox="0 0 36 36" className="source-svg-logo" aria-label="Google Scholar">
        <path d="M30.5 18.3c0-.9-.1-1.7-.3-2.5H18v4.8h7.1c-.3 1.6-1.2 3-2.6 3.9v3.2h4.2c2.5-2.3 3.9-5.7 3.9-9.4z" fill="#4285F4" />
        <path d="M18 31c3.6 0 6.6-1.2 8.8-3.2l-4.2-3.2c-1.2.8-2.7 1.3-4.6 1.3-3.5 0-6.5-2.4-7.6-5.6H6.1v3.3C8.3 27.8 12.8 31 18 31z" fill="#34A853" />
        <path d="M10.4 20.3c-.3-.9-.4-1.8-.4-2.8s.1-1.9.4-2.8v-3.3H6.1C5.2 13.2 4.7 15.5 4.7 18s.5 4.8 1.4 6.6l4.3-3.3z" fill="#FBBC05" />
        <path d="M18 9.1c2 0 3.7.7 5.1 2l3.8-3.8C24.6 5.2 21.6 4 18 4 12.8 4 8.3 7.2 6.1 11.4l4.3 3.3c1.1-3.2 4.1-5.6 7.6-5.6z" fill="#EA4335" />
      </svg>
    ),
  },
  {
    id: 'biorxiv',
    name: 'bioRxiv',
    url: 'https://www.biorxiv.org/',
    description: 'Cold Spring Harbor Laboratory preprint repository for biological sciences, delivering early AMR genomics findings.',
    records: 'Preprint Archive',
    logo: (
      <div className="source-brand-text">
        <span className="brand-dark">bio</span>
        <span className="brand-red">R</span>
        <span className="brand-dark">xiv</span>
      </div>
    ),
  },
  {
    id: 'medrxiv',
    name: 'medRxiv',
    url: 'https://www.medrxiv.org/',
    description: 'Cold Spring Harbor Laboratory preprint server for health and clinical sciences, covering emergent AMR isolates.',
    records: 'Clinical Preprints',
    logo: (
      <div className="source-brand-text">
        <span className="brand-dark">med</span>
        <span className="brand-red">R</span>
        <span className="brand-dark">xiv</span>
      </div>
    ),
  },
];

function IntegratedLiteratureSources({ onOpenModal }) {
  return (
    <section className="panel literature-sources-card" aria-label="Integrated Literature Sources">
      <div className="literature-sources-header">
        <div className="literature-sources-title-group">
          <span className="literature-sources-icon-badge">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>
              <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path>
              <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>
            </svg>
          </span>
          <h2 className="literature-sources-title">Integrated Literature Sources</h2>
        </div>
        <button
          type="button"
          className="view-all-sources-btn"
          onClick={onOpenModal}
          title="View federated research sources details"
        >
          View all sources <span className="chevron">&rsaquo;</span>
        </button>
      </div>

      <div className="literature-sources-grid">
        {LITERATURE_SOURCES.map((source) => (
          <a
            key={source.id}
            href={source.url}
            target="_blank"
            rel="noopener noreferrer"
            className="source-item-card"
            title={`${source.name}: ${source.records} — click to visit database portal`}
          >
            <div className="source-item-logo">{source.logo}</div>
            <div className="source-item-name">{source.name}</div>
            <div className="source-item-dot" title="Active & integrated into AMR research" />
          </a>
        ))}
      </div>
    </section>
  );
}

function SourcesModal({ onClose }) {
  return (
    <div className="modal-backdrop" onClick={onClose} role="dialog" aria-modal="true">
      <div className="sources-modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="sources-modal-header">
          <div>
            <h2 className="sources-modal-title">Integrated Literature Sources</h2>
            <p className="sources-modal-subtitle">
              AMR Nexus queries and synthesizes scientific evidence across 8 federated databases in parallel alongside CARD and NCBI AMRFinderPlus.
            </p>
          </div>
          <button type="button" className="sources-modal-close" onClick={onClose} aria-label="Close dialog">
            &times;
          </button>
        </div>
        <div className="sources-modal-list">
          {LITERATURE_SOURCES.map((s) => (
            <div key={s.id} className="source-modal-item">
              <div className="source-modal-logo-col">{s.logo}</div>
              <div className="source-modal-info-col">
                <div className="source-modal-name-row">
                  <span className="source-modal-name">{s.name}</span>
                  <span className="source-modal-badge">{s.records}</span>
                  <span className="source-modal-status-badge">● Active in Pipeline</span>
                </div>
                <p className="source-modal-desc">{s.description}</p>
                <a href={s.url} target="_blank" rel="noopener noreferrer" className="source-modal-link">
                  Visit database portal &rarr;
                </a>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const EXAMPLE_QUERY = "Which mutations in rpoB are associated with rifampicin resistance in Mycobacterium tuberculosis?";

export default function App() {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [showSourcesModal, setShowSourcesModal] = useState(false);

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
      </header>

      <main className="copilot-workspace">
        <section className="panel query-panel">
          <form onSubmit={submit}>
            <div className="query-header">
              <label htmlFor="query" className="query-label">
                AMR Research Query
              </label>
              <div className="example-query-wrapper">
                <button
                  type="button"
                  className="example-query-chip"
                  onClick={() => setQuery(EXAMPLE_QUERY)}
                  title="Click to populate question"
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
              placeholder="Ask AMR Nexus..."
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
                    <span className="btn-spinner" /> Searching…
                  </>
                ) : (
                  'Search'
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
                              {reference.source_provenance && reference.source_provenance.length > 0 ? (
                                reference.source_provenance.map((src) => (
                                  <span key={src} className="source-pill reference-source-tag">
                                    {src}
                                  </span>
                                ))
                              ) : reference.source ? (
                                <span className="source-pill reference-source-tag">
                                  {reference.source}
                                </span>
                              ) : null}
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
          <IntegratedLiteratureSources onOpenModal={() => setShowSourcesModal(true)} />
        )}
      </main>

      {showSourcesModal && <SourcesModal onClose={() => setShowSourcesModal(false)} />}
    </div>
  );
}
