import { Component, type ReactNode } from "react";

/**
 * Панель с данными неожиданной формы не должна ронять «Учёбу» (D-039): при исключении при отрисовке
 * панель скрывается, остальные остаются. Сообщение уходит в консоль для диагностики.
 */
export class SafePanel extends Component<{ children: ReactNode; panelId: string }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  override componentDidCatch(error: unknown) {
    console.warn(`Панель ${this.props.panelId} скрыта: не удалось отрисовать`, error);
  }

  override render() {
    return this.state.failed ? null : this.props.children;
  }
}
