import { Routes, Route, Navigate } from 'react-router-dom';
import AppLayout from './components/layout/AppLayout';
import DashboardPage from './pages/DashboardPage';
import InterpretPage from './pages/InterpretPage';
import GeneratePage from './pages/GeneratePage';
import CheckPage from './pages/CheckPage';
import FormatPage from './pages/FormatPage';
import NewsPage from './pages/NewsPage';
import SettingsPage from './pages/SettingsPage';
import LoginPage from './pages/LoginPage';
import { useAppStore } from './stores/appStore';

function PublicOnlyRoute({ children }: { children: React.ReactNode }) {
  const token = useAppStore((s) => s.token);
  const hydrated = useAppStore((s) => s._hydrated);

  if (!hydrated) {
    const lsToken = localStorage.getItem('bidmaster_token');
    if (lsToken) {
      return <Navigate to="/dashboard" replace />;
    }
    return <>{children}</>;
  }

  if (token) {
    return <Navigate to="/dashboard" replace />;
  }

  return <>{children}</>;
}

function PermissionRoute({ children, module }: { children: React.ReactNode; module: string }) {
  const user = useAppStore((s) => s.user);
  const permissions = useAppStore((s) => s.permissions) || [];
  const isSystemAdmin = user?.roles?.some(r => r.name === 'admin') ?? false;

  const hasPerm = isSystemAdmin || permissions.some(p => p.startsWith(module + '.'));

  if (!hasPerm) {
    return <Navigate to="/dashboard" replace />;
  }

  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<PublicOnlyRoute><LoginPage /></PublicOnlyRoute>} />
      <Route element={<AppLayout />}>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/interpret" element={<PermissionRoute module="interpret"><InterpretPage /></PermissionRoute>} />
        <Route path="/generate" element={<PermissionRoute module="generate"><GeneratePage /></PermissionRoute>} />
        <Route path="/check" element={<PermissionRoute module="check"><CheckPage /></PermissionRoute>} />
        <Route path="/format" element={<PermissionRoute module="format"><FormatPage /></PermissionRoute>} />
        <Route path="/news" element={<PermissionRoute module="news"><NewsPage /></PermissionRoute>} />
        <Route path="/settings" element={<PermissionRoute module="settings"><SettingsPage /></PermissionRoute>} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Route>
    </Routes>
  );
}
