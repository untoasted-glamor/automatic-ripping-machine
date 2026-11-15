import { describe, it, expect } from 'vitest';
import { changedPosition, identityOpensFor, numOrNull, positionOf } from '../identity-position';

const job = { season: 2, disc_number: 1, disc_total: 3 };

describe('changedPosition', () => {
	it('sends nothing when nothing changed', () => {
		expect(changedPosition(job, positionOf(job))).toEqual({});
	});
	it('sends only the changed fields, an emptied one as null', () => {
		expect(changedPosition(job, { season: 2, discNumber: 2, discTotal: null })).toEqual({
			disc_number: 2,
			disc_total: null
		});
	});
	it('reads a typed string as its number', () => {
		expect(changedPosition(job, { season: '3' as unknown as number, discNumber: 1, discTotal: 3 })).toEqual({
			season: 3
		});
	});
	it('never sends a season for a CD', () => {
		expect(changedPosition(job, { season: 9, discNumber: 1, discTotal: 3 }, { isCd: true })).toEqual({});
	});
	it('treats a missing job value as null', () => {
		expect(changedPosition({}, { season: null, discNumber: null, discTotal: null })).toEqual({});
	});
});

describe('numOrNull', () => {
	it.each([
		[null, null],
		[undefined, null],
		['', null],
		['abc', null],
		['4', 4],
		[0, 0]
	])('%s -> %s', (v, out) => {
		expect(numOrNull(v as never)).toBe(out);
	});
});

describe('identityOpensFor', () => {
	it.each([
		['awaiting_user_id', true],
		['ripped_awaiting_identify', true],
		['awaiting_review', true],
		['awaiting_titles', true],
		['identified', false],
		['ripping', false],
		['ripped', false]
	])('%s -> %s', (status, opens) => {
		expect(identityOpensFor(status)).toBe(opens);
	});
});
