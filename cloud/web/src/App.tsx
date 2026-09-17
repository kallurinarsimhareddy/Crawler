import { Link, Route, Routes } from "react-router-dom";
import { RequireAuth } from "./auth/RequireAuth";
import { EmptyState } from "./components/Feedback";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { JobDetail } from "./pages/JobDetail";
import { Jobs } from "./pages/Jobs";
import { Login } from "./pages/Login";
import { NewCrawl } from "./pages/NewCrawl";

export function App() {
  return (
    <Routes>
      <Route path="login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="new" element={<NewCrawl />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="jobs/:jobId" element={<JobDetail />} />
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
