import { Link, Route, Routes } from "react-router-dom";
import { RequireAuth } from "./auth/RequireAuth";
import { EmptyState } from "./components/Feedback";
import { Layout } from "./components/Layout";
import { JobDetail } from "./pages/JobDetail";
import { Jobs } from "./pages/Jobs";
import { Login } from "./pages/Login";
import { NewCrawl } from "./pages/NewCrawl";
import { Companies, CompanyDetail } from "./platform/pages/Companies";
import { ContactDetail, Contacts, Discovery, HiringIntel, Postings, Provenance } from "./platform/pages/Intel";
import { Credits, Exports, ImportDetail, Imports, Research, ResearchRun, ScrapeRun, Scraper, Settings, Sources } from "./platform/pages/Tools";
import { Analytics, BackgroundTasks, Dashboard, ListDetail, Opportunities } from "./platform/pages/Work";
import { ResourcePage } from "./platform/ResourcePage";
import { ACTIVITIES, CAMPAIGNS, LISTS, MONITORS, SEGMENTS, SEQUENCES, SUPPRESSIONS, TASKS, TEMPLATES, WORKFLOWS } from "./platform/resources";
import { RecordView, RequireWorkspace } from "./platform/Shell";
import { WorkspaceProvider } from "./platform/workspace";

function W({ children }: { children: React.ReactNode }) {
  return <RequireWorkspace>{children}</RequireWorkspace>;
}

export function App() {
  return (
    <Routes>
      <Route path="login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <WorkspaceProvider>
              <Layout />
            </WorkspaceProvider>
          </RequireAuth>
        }
      >
        {/* CareerCloud crawl pages, unchanged, under "Crawls" in the navigation. */}
        <Route path="new" element={<NewCrawl />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="jobs/:jobId" element={<JobDetail />} />

        {/* The platform. Every page below is scoped to the selected workspace. */}
        <Route index element={<W><Dashboard /></W>} />
        <Route path="companies" element={<W><Companies /></W>} />
        <Route path="companies/:companyId" element={<W><CompanyDetail /></W>} />
        <Route path="contacts" element={<W><Contacts /></W>} />
        <Route path="contacts/:contactId" element={<W><ContactDetail /></W>} />
        <Route path="postings" element={<W><Postings /></W>} />
        <Route path="opportunities" element={<W><Opportunities /></W>} />
        <Route path="opportunities/:id" element={<W><RecordView path="/opportunities" back="/opportunities" backLabel="Opportunities" /></W>} />
        <Route path="tasks" element={<W><ResourcePage config={TASKS} /></W>} />
        <Route path="activities" element={<W><ResourcePage config={ACTIVITIES} /></W>} />
        <Route path="hiring" element={<W><HiringIntel /></W>} />
        <Route path="discovery" element={<W><Discovery /></W>} />
        <Route path="scraper" element={<W><Scraper /></W>} />
        <Route path="scraper/:runId" element={<W><ScrapeRun /></W>} />
        <Route path="research" element={<W><Research /></W>} />
        <Route path="research/:runId" element={<W><ResearchRun /></W>} />
        <Route path="campaigns" element={<W><ResourcePage config={CAMPAIGNS} /></W>} />
        <Route path="sequences" element={<W><ResourcePage config={SEQUENCES} /></W>} />
        <Route path="templates" element={<W><ResourcePage config={TEMPLATES} /></W>} />
        <Route path="suppressions" element={<W><ResourcePage config={SUPPRESSIONS} /></W>} />
        <Route path="lists" element={<W><ResourcePage config={LISTS} /></W>} />
        <Route path="lists/:listId" element={<W><ListDetail /></W>} />
        <Route path="segments" element={<W><ResourcePage config={SEGMENTS} /></W>} />
        <Route path="workflows" element={<W><ResourcePage config={WORKFLOWS} /></W>} />
        <Route path="monitors" element={<W><ResourcePage config={MONITORS} /></W>} />
        <Route path="imports" element={<W><Imports /></W>} />
        <Route path="imports/:batchId" element={<W><ImportDetail /></W>} />
        <Route path="exports" element={<W><Exports /></W>} />
        <Route path="sources" element={<W><Sources /></W>} />
        <Route path="credits" element={<W><Credits /></W>} />
        <Route path="analytics" element={<W><Analytics /></W>} />
        <Route path="background" element={<W><BackgroundTasks /></W>} />
        <Route path="provenance/:entity/:entityId" element={<W><Provenance /></W>} />
        <Route path="settings" element={<W><Settings /></W>} />
        <Route
          path="*"
          element={
            <div className="page">
              <EmptyState title="Page not found">
                <Link to="/" className="button button--primary">
                  Back to dashboard
                </Link>
              </EmptyState>
            </div>
          }
        />
      </Route>
    </Routes>
  );
}
