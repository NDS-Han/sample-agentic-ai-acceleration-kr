// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** 공용 펼침 대상(disclosureProps) — 행 클릭·안쪽 요소·키보드·텍스트 선택·셰브런·포털. */

import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { createPortal } from 'react-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { disclosureProps } from '@/lib/utils/disclosure';

function Row({ onEdit = () => {}, portal = false }: { onEdit?: () => void; portal?: boolean }) {
  const [open, setOpen] = useState(false);
  const d = disclosureProps({ expanded: open, onToggle: () => setOpen((v) => !v), controlsId: 'detail-1' });
  return (
    <table>
      <tbody>
        <tr {...d.target} data-testid="row">
          <td>
            <button {...d.chevron} aria-label="Expand Kim">›</button>
          </td>
          <td>
            <span data-testid="name">Kim Example</span>
          </td>
          <td>
            <button type="button" onClick={onEdit}>Set budget</button>
            <a href="#x">Open</a>
            <input aria-label="limit" />
            <select aria-label="pick"><option>a</option></select>
            <input type="checkbox" aria-label="check" />
            {portal && createPortal(<button type="button">In dialog</button>, document.body)}
          </td>
        </tr>
        {open && (
          <tr id="detail-1">
            <td>detail</td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

const row = () => screen.getByTestId('row');
const isOpen = () => row().getAttribute('aria-expanded') === 'true';

afterEach(() => window.getSelection()?.removeAllRanges());

describe('disclosureProps', () => {
  it('행(이름) 클릭이 토글하고 aria-expanded·aria-controls 가 따라간다', async () => {
    render(<Row />);
    const user = userEvent.setup();
    expect(row().getAttribute('aria-controls')).toBe('detail-1');
    await user.click(screen.getByTestId('name'));
    expect(isOpen()).toBe(true);
    expect(document.getElementById('detail-1')).toBeTruthy();
    await user.click(row());
    expect(isOpen()).toBe(false);
  });

  it('안쪽 버튼·링크·입력·select·체크박스는 토글하지 않는다(자기 동작만)', async () => {
    const onEdit = vi.fn();
    render(<Row onEdit={onEdit} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Set budget' }));
    expect(onEdit).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole('link', { name: 'Open' }));
    await user.click(screen.getByRole('textbox', { name: 'limit' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'pick' }), 'a');
    await user.click(screen.getByRole('checkbox', { name: 'check' }));
    expect(isOpen()).toBe(false);
  });

  it('셰브런 버튼은 한 번만 토글(행으로 버블되어 두 번 토글되지 않음)하고 탭 순서에서 빠진다', async () => {
    render(<Row />);
    const user = userEvent.setup();
    const chev = screen.getByRole('button', { name: 'Expand Kim' });
    expect(chev.tabIndex).toBe(-1);
    await user.click(chev);
    expect(isOpen()).toBe(true);
    expect(chev.getAttribute('aria-expanded')).toBe('true');
    await user.click(chev);
    expect(isOpen()).toBe(false);
  });

  it('키보드 — 행이 포커스되고 Enter/Space 가 토글, 안쪽 입력의 Enter/Space 는 무시', async () => {
    render(<Row />);
    const user = userEvent.setup();
    await user.tab();
    expect(document.activeElement).toBe(row());
    expect(row().className).toContain('focus-visible:ring-2');
    await user.keyboard('{Enter}');
    expect(isOpen()).toBe(true);
    await user.keyboard(' ');
    expect(isOpen()).toBe(false);
    const input = screen.getByRole('textbox', { name: 'limit' });
    input.focus();
    await user.keyboard('{Enter} ');
    expect(isOpen()).toBe(false);
  });

  it('행 안 텍스트를 선택한 채 놓은 클릭은 토글하지 않는다', () => {
    render(<Row />);
    const name = screen.getByTestId('name');
    const range = document.createRange();
    range.selectNodeContents(name);
    window.getSelection()!.addRange(range);
    fireEvent.click(name);
    expect(isOpen()).toBe(false);
    window.getSelection()!.removeAllRanges();
    fireEvent.click(name);
    expect(isOpen()).toBe(true);
  });

  it('포털(다이얼로그 등)에서 버블된 클릭은 토글하지 않는다', async () => {
    render(<Row portal />);
    await userEvent.setup().click(screen.getByRole('button', { name: 'In dialog' }));
    expect(isOpen()).toBe(false);
  });

  it('disabled — 포커스·클릭 토글 없음', async () => {
    function D() {
      const d = disclosureProps({ expanded: false, onToggle: () => { throw new Error('toggled'); }, disabled: true });
      return <div {...d.target} data-testid="h">head</div>;
    }
    render(<D />);
    expect(screen.getByTestId('h').hasAttribute('tabindex')).toBe(false);
    await userEvent.setup().click(screen.getByTestId('h'));
  });
});
