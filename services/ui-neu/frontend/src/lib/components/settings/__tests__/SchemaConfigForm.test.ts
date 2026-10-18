import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import SchemaConfigForm from '../SchemaConfigForm.svelte';
import type { SettingsGroup } from '$lib/types/api.gen';

const saveArmConfig = vi.fn((_config: Record<string, unknown>) => Promise.resolve({ success: true }));
vi.mock('$lib/api/settings', () => ({ saveArmConfig: (config: Record<string, unknown>) => saveArmConfig(config) }));

const GROUP: SettingsGroup = {
  name: 'Metadata',
  fields: [
    { key: 'metadata_provider', group: 'Metadata', tier: 'operator', label: 'Provider', help: '', type: 'enum', editable: true, enum_values: ['tmdb', 'omdb'] },
    { key: 'tmdb_api_key', group: 'Metadata', tier: 'secret', label: 'TMDb key', help: '', type: 'string', editable: true, enum_values: null },
  ],
};
const CONFIG = { metadata_provider: 'tmdb', tmdb_api_key: '<hidden>' };

afterEach(() => { cleanup(); saveArmConfig.mockClear(); });

describe('SchemaConfigForm', () => {
  it('renders a control per editable field, seeded from config', async () => {
    renderComponent(SchemaConfigForm, { props: { group: GROUP, config: CONFIG } });
    expect((screen.getByRole('combobox', { name: /provider/i }) as HTMLSelectElement).value).toBe('tmdb');
    expect(screen.getByLabelText(/tmdb key/i)).toBeInTheDocument();
  });

  it('saves only changed fields, omitting an untouched <hidden> secret', async () => {
    renderComponent(SchemaConfigForm, { props: { group: GROUP, config: CONFIG } });
    await fireEvent.change(screen.getByRole('combobox', { name: /provider/i }), { target: { value: 'omdb' } });
    await fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saveArmConfig).toHaveBeenCalled());
    const payload = saveArmConfig.mock.calls[0][0];
    expect(payload.metadata_provider).toBe('omdb');
    expect('tmdb_api_key' in payload).toBe(false);
  });

  it('includes a secret in the payload when the user types a new value', async () => {
    renderComponent(SchemaConfigForm, { props: { group: GROUP, config: CONFIG } });
    await fireEvent.input(screen.getByLabelText(/tmdb key/i), { target: { value: 'new-key' } });
    await fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saveArmConfig).toHaveBeenCalled());
    expect(saveArmConfig.mock.calls[0][0].tmdb_api_key).toBe('new-key');
  });
});

describe('SchemaConfigForm review-gate countdown', () => {
  const RIPPING: SettingsGroup = {
    name: 'Ripping',
    fields: [
      { key: 'hold_for_review', group: 'Ripping', tier: 'operator', label: 'Hold discs for review', help: '', type: 'bool', editable: true, enum_values: null },
      { key: 'manual_wait_seconds', group: 'Ripping', tier: 'operator', label: 'Review countdown (seconds)', help: 'Leave blank to never auto-start.', type: 'int', editable: true, enum_values: null },
    ],
  };

  function renderRipping(config: Record<string, unknown>) {
    renderComponent(SchemaConfigForm, { props: { group: RIPPING, config } });
  }

  it('hides the countdown control while hold-for-review is off', () => {
    renderRipping({ hold_for_review: false, manual_wait_seconds: 60 });
    expect(screen.queryByTestId('review-wait-field')).not.toBeInTheDocument();
  });

  it('shows the countdown once hold-for-review is ticked', async () => {
    renderRipping({ hold_for_review: false, manual_wait_seconds: 60 });
    await fireEvent.click(screen.getByLabelText('Hold discs for review'));
    expect((screen.getByTestId('manual-wait-seconds') as HTMLInputElement).value).toBe('60');
    expect(screen.getByTestId('mandatory-confirm')).not.toBeChecked();
  });

  it('sends the edited countdown as a number', async () => {
    renderRipping({ hold_for_review: true, manual_wait_seconds: 60 });
    const field = screen.getByTestId('manual-wait-seconds');
    // Clear first, the way a person retyping the value does.
    await fireEvent.input(field, { target: { value: '' } });
    await fireEvent.input(field, { target: { value: '120' } });
    await fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saveArmConfig).toHaveBeenCalled());
    expect(saveArmConfig.mock.calls[0][0]).toEqual({ manual_wait_seconds: 120 });
  });

  it('blocks the save while the countdown box is empty instead of PATCHing ""', async () => {
    renderRipping({ hold_for_review: true, manual_wait_seconds: 60 });
    await fireEvent.input(screen.getByTestId('manual-wait-seconds'), { target: { value: '' } });
    expect(screen.getByTestId('manual-wait-seconds-error')).toBeInTheDocument();
    const save = screen.getByRole('button', { name: /save/i });
    expect(save).toBeDisabled();
    await fireEvent.click(save);
    expect(saveArmConfig).not.toHaveBeenCalled();
  });

  it('rejects a non-positive countdown', async () => {
    renderRipping({ hold_for_review: true, manual_wait_seconds: 60 });
    await fireEvent.input(screen.getByTestId('manual-wait-seconds'), { target: { value: '0' } });
    expect(screen.getByTestId('manual-wait-seconds-error')).toHaveTextContent(/at least 1/);
  });

  it('keeps mandatory mode with the countdown hidden and resends nothing unchanged', async () => {
    renderRipping({ hold_for_review: true, manual_wait_seconds: null });
    expect(screen.getByTestId('mandatory-confirm')).toBeChecked();
    expect(screen.queryByTestId('manual-wait-seconds')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saveArmConfig).toHaveBeenCalled());
    expect(saveArmConfig.mock.calls[0][0]).toEqual({});
  });

  it('ticking "Require manual confirm" saves null', async () => {
    renderRipping({ hold_for_review: true, manual_wait_seconds: 90 });
    await fireEvent.click(screen.getByTestId('mandatory-confirm'));
    expect(screen.queryByTestId('manual-wait-seconds')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saveArmConfig).toHaveBeenCalled());
    expect(saveArmConfig.mock.calls[0][0]).toEqual({ manual_wait_seconds: null });
  });

  it('unticking "Require manual confirm" restores a 60s countdown', async () => {
    renderRipping({ hold_for_review: true, manual_wait_seconds: null });
    await fireEvent.click(screen.getByTestId('mandatory-confirm'));
    expect((screen.getByTestId('manual-wait-seconds') as HTMLInputElement).value).toBe('60');
    await fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saveArmConfig).toHaveBeenCalled());
    expect(saveArmConfig.mock.calls[0][0]).toEqual({ manual_wait_seconds: 60 });
  });

  it('does not block or send an invalid countdown while the gate is off', async () => {
    renderRipping({ hold_for_review: true, manual_wait_seconds: 60 });
    await fireEvent.input(screen.getByTestId('manual-wait-seconds'), { target: { value: '' } });
    await fireEvent.click(screen.getByLabelText('Hold discs for review'));
    await fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => expect(saveArmConfig).toHaveBeenCalled());
    expect(saveArmConfig.mock.calls[0][0]).toEqual({ hold_for_review: false });
  });
});
