import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { RequireAuth } from "./auth/RequireAuth";
import { EmptyState } from "./components/Feedback";
import { Layout } from "./components/Layout";
import { JobDetail } from "./pages/JobDetail";
import { Jobs } from "./pages/Jobs";
import { Invite } from "./pages/Invite";
import { Login } from "./pages/Login";
import { NewCrawl } from "./pages/NewCrawl";
import { ResetPassword } from "./pages/ResetPassword";
import { CompanyDetail } from "./platform/pages/Companies";
import { ContactDetail, Discovery, HiringIntel, Provenance } from "./platform/pages/Intel";
import { JobView, JobsPage } from "./platform/pages/JobFeed";
import { JobImportPage } from "./platform/pages/JobImport";
import { JobKeywordsPage } from "./platform/pages/JobKeywords";
import { MonitorDetail, MonitorsPage } from "./platform/pages/JobMonitors";
import { Home } from "./platform/pages/Home";
import { Analytics } from "./platform/pages/Analytics";
import { AuditLog, Integrations, Notifications, UsersPermissions } from "./platform/pages/Admin";
import { EmailValidation, EmailValidationJob } from "./platform/pages/EmailValidation";
import { InternalData } from "./platform/pages/InternalData";
import { CampaignDetail, EmailSending, SequenceDetail, Suppressions } from "./platform/pages/Sending";
import { CampaignsSection, CompaniesSection, ContactsSection, HiringSection, ProspectingSection, ResearchSection, SequencesSection, SettingsSection } from "./platform/pages/Sections";
import { Credits, Exports, ImportDetail, Imports, ResearchRun, Sources } from "./platform/pages/Tools";
import { ScrapeRun, Scraper } from "./platform/pages/Scraper";
import { BackgroundTasks, Dashboard, ListDetail, Opportunities } from "./platform/pages/Work";
import { ResourcePage } from "./platform/ResourcePage";
import { ACTIVITIES, LISTS, MONITORS, SEGMENTS, TASKS, TEMPLATES } from "./platform/resources";
import { RecordView, RequireWorkspace } from "./platform/Shell";
import { AutomationBuilder } from "./platform/controlroom/Automation";
import { ControlRoom } from "./platform/controlroom/ControlRoom";
import { MemoryPage } from "./platform/controlroom/Memory";
import { WorkspaceProvider } from "./platform/workspace";

function W({ children }: { children: React.ReactNode }) {
  return <RequireWorkspace>{children}</RequireWorkspace>;
}

/** /postings moved to /jobs (D1); old links keep their filters. */
function PostingsRedirect() {
  const { search } = useLocation();
  return <Navigate to={`/jobs${search}`} replace />;
}

export function App() {
  return (
    <Routes>
      <Route path="login" element={<Login />} />
      <Route path="reset-password" element={<ResetPassword />} />
      {/* Public: shows the invitation before sign-in, accepts it after. */}
      <Route path="invite" element={<Invite />} />
      <Route path="invite/:token" element={<Invite />} />
      <Route
        element={
          <RequireAuth>
            <WorkspaceProvider>
              <Layout />
            </WorkspaceProvider>
          </RequireAuth>
        }
      >
        {/* CareerCloud crawl pages, unchanged, under Settings → Crawls. */}
        <Route path="new" element={<NewCrawl />} />
        <Route path="settings/crawls" element={<Jobs />} />
        <Route path="settings/crawls/:jobId" element={<JobDetail />} />

        {/* The platform. Every page below is scoped to the selected workspace. */}
        <Route index element={<W><Home /></W>} />
        <Route path="ai" element={<W><ControlRoom /></W>} />
        <Route path="dashboard" element={<W><Dashboard /></W>} />
        <Route path="ai/memory" element={<W><MemoryPage /></W>} />
        <Route path="companies" element={<W><CompaniesSection /></W>} />
        <Route path="companies/:companyId" element={<W><CompanyDetail /></W>} />
        <Route path="contacts" element={<W><ContactsSection /></W>} />
        <Route path="contacts/:contactId" element={<W><ContactDetail /></W>} />
        <Route path="postings" element={<PostingsRedirect />} />
        <Route path="jobs" element={<W><JobsPage /></W>} />
        <Route path="jobs/import" element={<W><JobImportPage /></W>} />
        <Route path="jobs/keywords" element={<W><JobKeywordsPage /></W>} />
        <Route path="jobs/:jobId" element={<W><JobView /></W>} />
        <Route path="opportunities" element={<W><Opportunities /></W>} />
        <Route path="opportunities/:id" element={<W><RecordView path="/opportunities" back="/opportunities" backLabel="Opportunities" /></W>} />
        <Route path="tasks" element={<W><ResourcePage config={TASKS} /></W>} />
        <Route path="activities" element={<W><ResourcePage config={ACTIVITIES} /></W>} />
        <Route path="hiring" element={<W><HiringSection /></W>} />
        <Route path="signals" element={<W><HiringIntel title="Signals" /></W>} />
        <Route path="prospecting" element={<W><ProspectingSection /></W>} />
        <Route path="discovery" element={<W><Discovery /></W>} />
        <Route path="scraper" element={<W><Scraper /></W>} />
        <Route path="scraper/:runId" element={<W><ScrapeRun /></W>} />
        <Route path="research" element={<W><ResearchSection /></W>} />
        <Route path="research/:runId" element={<W><ResearchRun /></W>} />
        <Route path="campaigns" element={<W><CampaignsSection /></W>} />
        <Route path="campaigns/:campaignId" element={<W><CampaignDetail /></W>} />
        <Route path="sequences" element={<W><SequencesSection /></W>} />
        <Route path="sequences/:sequenceId" element={<W><SequenceDetail /></W>} />
        <Route path="email-validation" element={<W><EmailValidation /></W>} />
        <Route path="email-validation/:jobId" element={<W><EmailValidationJob /></W>} />
        <Route path="templates" element={<W><ResourcePage config={TEMPLATES} /></W>} />
        <Route path="suppressions" element={<W><Suppressions /></W>} />
        <Route path="lists" element={<W><ResourcePage config={LISTS} /></W>} />
        <Route path="lists/:listId" element={<W><ListDetail /></W>} />
        <Route path="segments" element={<W><ResourcePage config={SEGMENTS} /></W>} />
        <Route path="workflows" element={<W><AutomationBuilder /></W>} />
        <Route path="monitors" element={<W><MonitorsPage /></W>} />
        <Route path="monitors/:monitorId" element={<W><MonitorDetail /></W>} />
        <Route path="change-monitors" element={<W><ResourcePage config={MONITORS} /></W>} />
        <Route path="imports" element={<W><Imports /></W>} />
        <Route path="imports/:batchId" element={<W><ImportDetail /></W>} />
        <Route path="internal-data" element={<W><InternalData /></W>} />
        <Route path="exports" element={<W><Exports /></W>} />
        <Route path="sources" element={<W><Sources /></W>} />
        <Route path="credits" element={<W><Credits /></W>} />
        <Route path="analytics" element={<W><Analytics /></W>} />
        <Route path="notifications" element={<W><Notifications /></W>} />
        <Route path="background" element={<W><BackgroundTasks /></W>} />
        <Route path="provenance/:entity/:entityId" element={<W><Provenance /></W>} />
        <Route path="settings" element={<W><SettingsSection /></W>} />
        <Route path="settings/sending" element={<W><EmailSending /></W>} />
        <Route path="settings/integrations" element={<W><Integrations /></W>} />
        <Route path="settings/users" element={<W><UsersPermissions /></W>} />
        <Route path="settings/audit" element={<W><AuditLog /></W>} />
        <Route
          path="*"
          element={
            <div className="page">
              <EmptyState
                icon="search"
                title="Page not found"
                description="This page does not exist or has moved. Use Ctrl+K to search for a page, company or contact."
                action={
                  <Link to="/" className="button button--primary">
                    Go to Home
                  </Link>
                }
              />
            </div>
          }
        />
      </Route>
    </Routes>
  );
}
