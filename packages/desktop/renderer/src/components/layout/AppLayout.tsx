import { useEffect } from 'react';
import { Outlet, Navigate } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';
import { useAppStore } from '../../stores/appStore';
import { projectApi, authApi } from '../../services/api';

export default function AppLayout() {
  const token = useAppStore((s) => s.token);
  const hydrated = useAppStore((s) => s._hydrated);
  const setProjects = useAppStore((s) => s.setProjects);
  const setUser = useAppStore((s) => s.setUser);
  const setPermissions = useAppStore((s) => s.setPermissions);

  // 页面刷新时重新获取用户信息和权限
  useEffect(() => {
    if (!token) return;
    let aborted = false;
    authApi.me()
      .then((res) => {
        if (aborted) return;
        const user = res.data;
        setUser(user);
        setPermissions(user.permissions || []);
      })
      .catch(() => {
        // token 无效时会被拦截器处理跳转登录页
      });
    return () => { aborted = true; };
  }, [token, setUser, setPermissions]);

  useEffect(() => {
    let aborted = false;
    projectApi.list()
      .then((res) => {
        if (aborted) return;
        const list = res.data?.projects || res.data || [];
        setProjects(Array.isArray(list) ? list : []);
      })
      .catch(() => {
        if (!aborted) setProjects([]);
      });
    return () => { aborted = true; };
  }, [setProjects]);

  if (!hydrated) {
    const lsToken = localStorage.getItem('bidmaster_token');
    if (!lsToken) {
      return <Navigate to="/login" replace />;
    }
  } else if (!token) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      <Sidebar />
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', background: 'var(--color-bg)' }}>
        <Header />
        <main style={{ flex: 1, overflow: 'auto', padding: '20px 24px', background: 'var(--color-bg)' }}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
