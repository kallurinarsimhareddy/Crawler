import { useMemo, useState, type ChangeEvent, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { JOB_TYPES, type JobCreateRequest, type JobType } from "../api/types";
import { ErrorBanner } from "../components/Feedback";
import { JOB_TYPE_DESCRIPTIONS, JOB_TYPE_LABELS, parseCompanyLines } from "../lib/format";
import { WorkerBanner, useWorkerStatus } from "../components/WorkerStatus";

const MAX_FILE_BYTES = 2 * 1024 * 1024;

export function NewCrawl() {
  const navigate = useNavigate();
  const [type, setType] = useState<JobType>("single_company");
  const [website, setWebsite] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [bulkText, setBulkText] = useState("");
  const [fileNote, setFileNote] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const { data: workerStatus, error: workerError } = useWorkerStatus();

  const bulkCompanies = useMemo(() => parseCompanyLines(bulkText), [bulkText]);
  const needsCompany = type === "single_company" || type === "discovery";

  const canSubmit =
    !submitting &&
    (type === "weekly_crawl" ||
      (type === "bulk_companies" && bulkCompanies.length > 0) ||
      (needsCompany && (website.trim() !== "" || companyName.trim() !== "")));

  function buildRequest(): JobCreateRequest {
    if (type === "weekly_crawl") return { type };
    if (type === "bulk_companies") return { type, companies: bulkCompanies };
    return {
      type,
      ...(website.trim() ? { website: website.trim() } : {}),
      ...(companyName.trim() ? { company_name: companyName.trim() } : {}),
    };
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.createJob(buildRequest());
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(err as Error);
      setSubmitting(false);
    }
  }

  async function onFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > MAX_FILE_BYTES) {
      setFileNote(`${file.name} is larger than 2 MB.`);
      return;
    }
    const text = await file.text();
    setBulkText(text);
    setFileNote(`Loaded ${file.name}`);
  }

  return (
    <div className="page page--narrow">
      <div className="page__header">
        <div>
          <h1>New crawl</h1>
          <p className="muted">Choose what to crawl, then run it.</p>
        </div>
      </div>

      <WorkerBanner status={workerStatus} error={workerError} />

      <form className="card form" onSubmit={onSubmit} noValidate>
        <fieldset className="field">
          <legend className="field__label">Crawl type</legend>
          <div className="choice-grid">
            {JOB_TYPES.map((option) => (
              <label key={option} className={`choice${type === option ? " choice--selected" : ""}`}>
                <input type="radio" name="type" value={option} checked={type === option} onChange={() => setType(option)} />
                <span className="choice__title">{JOB_TYPE_LABELS[option]}</span>
                <span className="choice__body">{JOB_TYPE_DESCRIPTIONS[option]}</span>
              </label>
            ))}
          </div>
        </fieldset>

        {needsCompany && (
          <div className="field-row">
            <div className="field">
              <label className="field__label" htmlFor="website">
                Website
              </label>
              <input
                id="website"
                className="input"
                type="text"
                inputMode="url"
                autoComplete="url"
                placeholder="example.com"
                value={website}
                onChange={(event) => setWebsite(event.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="company">
                Company name <span className="muted">(optional if website given)</span>
              </label>
              <input
                id="company"
                className="input"
                type="text"
                autoComplete="organization"
                placeholder="Example Inc."
                value={companyName}
                onChange={(event) => setCompanyName(event.target.value)}
              />
            </div>
          </div>
        )}

        {type === "bulk_companies" && (
          <div className="field">
            <div className="field__label-row">
              <label className="field__label" htmlFor="bulk">
                Companies
              </label>
              <label className="button button--ghost button--small file-button">
                Upload CSV
                <input type="file" accept=".csv,.txt,text/csv,text/plain" onChange={onFile} />
              </label>
            </div>
            <textarea
              id="bulk"
              className="input textarea"
              rows={8}
              placeholder={"One per line: a website, a name, or both\nacme.com\nGlobex Corporation, globex.com"}
              value={bulkText}
              onChange={(event) => setBulkText(event.target.value)}
            />
            <p className="field__hint">
              {bulkCompanies.length} {bulkCompanies.length === 1 ? "company" : "companies"} detected
              {fileNote ? ` · ${fileNote}` : ""}
            </p>
          </div>
        )}

        {(type === "weekly_crawl" || type === "discovery") && (
          <p className="alert alert--info">
            The cloud runner does not run {type === "weekly_crawl" ? "weekly roster crawls" : "discovery jobs"} yet. The job will be
            recorded but will not start.
          </p>
        )}

        {needsCompany && type === "single_company" && (
          <p className="field__hint">A website is required to crawl. Companies given by name only are recorded and skipped.</p>
        )}

        {error && <ErrorBanner error={error} />}

        <div className="form__actions">
          <button type="submit" className="button button--primary button--large" disabled={!canSubmit}>
            {submitting ? "Starting…" : "Run crawl"}
          </button>
        </div>
      </form>
    </div>
  );
}
