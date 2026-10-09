import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { NumberInput } from '@/components/common/NumberInput';

describe('NumberInput — 휠 스크롤 값 변동 차단', () => {
  it('wheel 이벤트 시 blur 되어 값이 변하지 않는다', () => {
    const onChange = vi.fn();
    render(<NumberInput aria-label="price" defaultValue="3" onChange={onChange} />);
    const input = screen.getByLabelText('price');

    input.focus();
    expect(document.activeElement).toBe(input);
    fireEvent.wheel(input, { deltaY: -100 });

    // blur 되어 이후 wheel 이 input 에 도달하지 않는다 — 값 변동 사고 차단.
    expect(document.activeElement).not.toBe(input);
    expect(input).toHaveValue(3);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('type="number" 와 나머지 속성은 네이티브 input 과 동일하게 동작한다', () => {
    render(<NumberInput aria-label="n" min={0} step={0.001} required />);
    const input = screen.getByLabelText('n') as HTMLInputElement;
    expect(input.type).toBe('number');
    expect(input.min).toBe('0');
    expect(input.required).toBe(true);
  });

  it('호출자의 onWheel 핸들러도 함께 호출된다', () => {
    const onWheel = vi.fn();
    render(<NumberInput aria-label="n" onWheel={onWheel} />);
    const input = screen.getByLabelText('n');
    fireEvent.wheel(input, { deltaY: 100 });
    expect(onWheel).toHaveBeenCalled();
  });
});
