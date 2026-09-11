// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { render, screen } from '@testing-library/react';
import { KPICard } from '@/components/common/KPICard';

describe('KPICard', () => {
  it('renders title and value', () => {
    render(<KPICard title="테스트" value={42} icon={<span>icon</span>} />);
    expect(screen.getByText('테스트')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
  });

  // Glass 디자인 시스템 도입 후 경고 보더는 raw Tailwind 색(border-red-500/border-yellow-500)이
  // 아니라 시맨틱 토큰(border-destructive/border-warning)을 쓴다. 다크/라이트 테마에서 색이
  // 갈리지 않게 하려는 의도된 변경이므로, 테스트도 토큰을 기준으로 고정한다.
  it('applies CRITICAL alert style', () => {
    const { container } = render(
      <KPICard title="위험" value="100%" icon={<span>icon</span>} alertLevel="CRITICAL" />
    );
    expect(container.firstChild).toHaveClass('border-destructive');
  });

  it('applies WARNING alert style', () => {
    const { container } = render(
      <KPICard title="경고" value="85%" icon={<span>icon</span>} alertLevel="WARNING" />
    );
    expect(container.firstChild).toHaveClass('border-warning');
  });

  it('NORMAL 은 강조 보더를 덮어쓰지 않는다', () => {
    const { container } = render(
      <KPICard title="정상" value="10%" icon={<span>icon</span>} alertLevel="NORMAL" />
    );
    const cls = (container.firstChild as HTMLElement).className;
    expect(cls).not.toContain('border-destructive');
    expect(cls).not.toContain('border-warning');
  });
});
