// ISO 639-2 language codes for transcode track selection. Mirrors
// arm_common/languages.py: a terminology code (deu, fra) is stored as its
// bibliographic form (ger, fre), the form Matroska tags and MakeMKV use. The
// backend normalises as well; the UI does it too so the list never shows
// one language twice.

export interface LanguageOption {
	code: string;
	name: string;
}

// The picker's common languages, in display order. Any other ISO 639-2 code
// can be typed in.
export const COMMON_LANGUAGES: LanguageOption[] = [
	{ code: 'eng', name: 'English' },
	{ code: 'jpn', name: 'Japanese' },
	{ code: 'ger', name: 'German' },
	{ code: 'fre', name: 'French' },
	{ code: 'spa', name: 'Spanish' },
	{ code: 'ita', name: 'Italian' },
	{ code: 'por', name: 'Portuguese' },
	{ code: 'dut', name: 'Dutch' },
	{ code: 'swe', name: 'Swedish' },
	{ code: 'nor', name: 'Norwegian' },
	{ code: 'dan', name: 'Danish' },
	{ code: 'fin', name: 'Finnish' },
	{ code: 'pol', name: 'Polish' },
	{ code: 'cze', name: 'Czech' },
	{ code: 'hun', name: 'Hungarian' },
	{ code: 'rus', name: 'Russian' },
	{ code: 'ukr', name: 'Ukrainian' },
	{ code: 'gre', name: 'Greek' },
	{ code: 'tur', name: 'Turkish' },
	{ code: 'ara', name: 'Arabic' },
	{ code: 'heb', name: 'Hebrew' },
	{ code: 'hin', name: 'Hindi' },
	{ code: 'chi', name: 'Chinese' },
	{ code: 'kor', name: 'Korean' },
	{ code: 'tha', name: 'Thai' },
	{ code: 'vie', name: 'Vietnamese' },
	{ code: 'ind', name: 'Indonesian' },
	{ code: 'und', name: 'Undetermined (no language tag)' }
];

const TERMINOLOGY_TO_BIBLIOGRAPHIC: Record<string, string> = {
	bod: 'tib',
	ces: 'cze',
	cym: 'wel',
	deu: 'ger',
	ell: 'gre',
	eus: 'baq',
	fas: 'per',
	fra: 'fre',
	hye: 'arm',
	isl: 'ice',
	kat: 'geo',
	mkd: 'mac',
	mri: 'mao',
	msa: 'may',
	mya: 'bur',
	nld: 'dut',
	ron: 'rum',
	slk: 'slo',
	sqi: 'alb',
	zho: 'chi'
};

const CODE_RE = /^[a-z]{3}$/;

/** The stored form of a typed code, or null when it is not a three-letter code. */
export function canonicalLanguage(raw: string): string | null {
	const value = raw.trim().toLowerCase();
	if (!CODE_RE.test(value)) return null;
	return TERMINOLOGY_TO_BIBLIOGRAPHIC[value] ?? value;
}

/** A readable label: the common name when known, the code otherwise. */
export function languageLabel(code: string): string {
	return COMMON_LANGUAGES.find((l) => l.code === code)?.name ?? code;
}
