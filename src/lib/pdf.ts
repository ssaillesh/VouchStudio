// PDF text for the engine. The local Studio uses `pdftotext -layout`, which Vercel lacks, so
// hosted PDF uploads are read with pdf.js (unpdf). Line breaks can differ slightly, so a PDF
// may score a little differently here than locally; .md and .txt uploads score identically.
import { extractText, getDocumentProxy } from 'unpdf';
import { HttpError } from './http';

export async function pdfText(bytes: Uint8Array): Promise<string> {
  try {
    const pdf = await getDocumentProxy(new Uint8Array(bytes));
    const { text } = await extractText(pdf, { mergePages: false });
    return text.join('\n');
  } catch {
    throw new HttpError(422, 'The PDF could not be read (it may be encrypted or damaged)');
  }
}
