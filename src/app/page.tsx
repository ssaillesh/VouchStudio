import { ClosingCta, HeroActions, HeroStats, SiteActions } from '@/components/site';
import { Brand, MarkSvg, RevealAll } from '@/components/ui';

export default function Home() {
  return (
    <>
      <RevealAll />
      <header className="topbar site-bar">
        <Brand />
        <SiteActions />
      </header>

      <main className="site-main">
        <section className="hero reveal">
          <p className="tagline">Better jobs. Smarter search.</p>
          <h1>Find the jobs your experience actually covers.</h1>
          <p className="lede">Upload your resume or a tagged master profile. VouchStudio reads the requirements
            of every posting on the board and scores each one against the evidence you have, so you see
            where you fit, and what is missing, before you apply.</p>
          <HeroActions />
          <HeroStats />
        </section>

        <section className="preview reveal" aria-label="What a scored job looks like">
          <div className="window">
            <div className="window-bar" aria-hidden="true"><span /><span /><span />
              <em>VouchStudio · your board</em></div>
            <div className="window-body">
              <article className="job-row demo">
                <span className="score proceed">81<small>9 reqs</small></span>
                <div className="grow">
                  <div className="title">Machine Learning Engineer, Link</div>
                  <div className="sub">Stripe · New York City · posted 2026-09-21</div>
                  <div className="tags"><span className="chip pass tag">65+</span><span className="chip accent tag">US</span>
                    <span className="chip tag">Mid or unspecified</span><span className="chip tag">Greenhouse</span></div>
                  <div className="detail">
                    <div><h4>Evidence matched</h4><div className="chips">
                      <span className="chip pass">Machine learning</span><span className="chip pass">Python</span>
                      <span className="chip pass">SQL</span><span className="chip pass">Statistics</span>
                      <span className="chip pass">ETL / data pipelines</span></div></div>
                    <div><h4>Gaps</h4><div className="chips">
                      <span className="chip">Ruby on Rails<em>missing</em></span>
                      <span className="chip">6+ years of experience<em>unclear</em></span></div></div>
                  </div>
                </div>
              </article>
            </div>
          </div>
        </section>

        <section className="site-section">
          <div className="section-intro reveal">
            <p className="eyebrow">How it works</p>
            <h2>Three steps, no guesswork.</h2>
          </div>
          <div className="bento steps">
            <div className="card span-3 reveal">
              <span className="step-num">01</span>
              <h3 className="step-title">Upload what you have</h3>
              <p>A plain resume works as it is: PDF, .txt or .md. A master profile with <code>[V]</code> and{' '}
                <code>[NO]</code> tags is stricter, because lines you mark <code>[NO]</code> never count.</p>
            </div>
            <div className="card span-3 reveal">
              <span className="step-num">02</span>
              <h3 className="step-title">Every posting, read the same way</h3>
              <p>Requirements are pulled from each description and weighted: 3 for must-haves, 2 for
                required skills, 1 for nice-to-haves.</p>
            </div>
            <div className="card span-6 reveal">
              <span className="step-num">03</span>
              <h3 className="step-title">A score you can check</h3>
              <p>Each job shows the evidence it matched and the gaps it found. The score is the share of
                the posting&apos;s weight your profile backs, so a 78 tells you exactly what is behind it.</p>
            </div>
          </div>
        </section>

        <section className="site-section">
          <div className="section-intro reveal">
            <p className="eyebrow">What makes it different</p>
            <h2>Built to be trusted.</h2>
          </div>
          <dl className="principles reveal">
            <div><dt>Evidence, not keywords</dt>
              <dd>A skill counts only when a line of your profile supports it. Knowing Docker never
                implies Kubernetes, and AWS never implies Azure.</dd></div>
            <div><dt>Rules, not a language model</dt>
              <dd>Scoring is deterministic: the same profile and posting always give the same score,
                and nothing you upload is sent to an AI service.</dd></div>
            <div><dt>Your profile stays yours</dt>
              <dd>It is stored with your account and used only to score jobs for you. Replace it, or
                delete your account and everything in it, whenever you like.</dd></div>
          </dl>
        </section>

        <section className="site-section">
          <div className="section-intro reveal">
            <p className="eyebrow">Plans</p>
            <h2>Score every job. Then build the resume.</h2>
          </div>
          <div className="plans">
            <div className="card reveal">
              <span className="chip tag">Basic</span>
              <h3>Know where you fit</h3>
              <p>Free with every account.</p>
              <ul className="pro-points">
                <li>Every job on the board scored against your profile</li>
                <li>The evidence each job matched and the gaps it found</li>
                <li>Filters by level, region and source</li>
                <li>Reviews of companies from other members, with photos, PDFs and video</li>
              </ul>
              <a className="btn" href="/signup">Create an account</a>
            </div>
            <div className="card pro reveal">
              <span className="chip tag pass">Pro</span>
              <h3>A resume for every job</h3>
              <p>Request it from your profile page; your account is switched once approved.</p>
              <ul className="pro-points">
                <li>A one-page resume tailored to each job, built from your master profile</li>
                <li>Every line traced to a fact you verified; nothing invented</li>
                <li>Checked to compile, fit one page and read cleanly in applicant tracking systems</li>
                <li>A report of what the job asks and the top changes that would raise your fit</li>
              </ul>
              <a className="btn primary" href="/signup">Start with Basic, ask for Pro</a>
            </div>
          </div>
        </section>

        <section className="site-section faq reveal">
          <h2>Questions</h2>
          <details><summary>What is a master profile?</summary>
            <p>A Markdown file of everything you have done, one fact per line, each tagged <code>[V]</code>{' '}
              when you can back it up or <code>[NO]</code> when you must never claim it. VouchStudio treats it
              as the source of truth, so it scores more strictly than a plain resume.</p></details>
          <details><summary>Who can see my resume?</summary>
            <p>Only you. It is kept with your account, used to score the board for you, and never shown to
              other accounts. Deleting your account deletes it.</p></details>
          <details><summary>Does it write my resume for me?</summary>
            <p>With Pro, yes. From a tagged master profile with a resume kit, VouchStudio picks the bullets that
              best cover each job, builds a one-page PDF and checks it. Every line traces back to a fact
              you marked as verified, so it never claims something you have not done.</p></details>
          <details><summary>Who can see my reviews?</summary>
            <p>Every signed-in member, under the name on your account (or &quot;A VouchStudio member&quot; if
              you left it blank). Your email is never shown. You can delete a review, and its files, at any time.</p></details>
          <details><summary>Which jobs are on the board?</summary>
            <p>Public postings from company job boards (Greenhouse, Ashby and others) across the United
              States and Canada, refreshed regularly. Listings without a full description are shown
              but not scored.</p></details>
        </section>

        <section className="closing brand-band reveal">
          <MarkSvg className="mark band-mark" />
          <h2>See your matches in a few minutes.</h2>
          <p className="tagline">Better jobs. Smarter search.</p>
          <ClosingCta />
        </section>
      </main>

      <footer className="foot site-foot">
        <Brand small />
        <span className="tagline">Better jobs. Smarter search.</span>
        <span>Scores are coverage of a posting&apos;s asks, not a prediction of being hired.</span>
      </footer>
    </>
  );
}
