import ThreePanelLayout from '@/components/layout/ThreePanelLayout';
import AuthGuard from '@/components/auth/AuthGuard';

export default function AppPage() {
  return (
    <AuthGuard>
      <ThreePanelLayout />
    </AuthGuard>
  );
}
