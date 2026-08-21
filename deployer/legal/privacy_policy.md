# PRIVACY POLICY

**ARGUS** — Last updated: 2026-07-11

## 1. Summary

ARGUS runs entirely on your computer and talks directly to Amazon Web Services (AWS). We designed it so that we receive as little of your data as possible. By default, we receive none.

## 2. What the app stores on your machine

The app stores your AWS access key ID and secret access key so it can act on your AWS account at your direction. These keys are encrypted at rest using your operating system's credential store (Windows Credential Manager). They never leave your computer except when sent directly to AWS to perform the actions you request. They are never transmitted to us, and we cannot see them.

The app also stores your deployment settings (deployment name, detection target, AWS region, feature toggles) locally on your machine so it can resume or manage your deployment.

## 3. What the app never touches

- **Payment card data.** If you add a card during AWS account setup, you type it into AWS's own signup page, shown inside the app's embedded browser. The card data goes directly from that page to AWS. The app does not read, capture, store, or transmit it, and we never see it.
- **Your AWS account password.** You enter it only into AWS's own pages. The app does not read or store it.
- **Your images and detection data.** These flow between your cameras, your computer, and your AWS account. We have no access to them.

## 4. Data sent to AWS

To deploy and operate your detection system, the app sends configuration commands, your access keys for authentication, and your uploaded training images directly to AWS over encrypted connections (TLS). This data goes into your own AWS account. AWS's handling of it is governed by AWS's privacy policy and your agreement with AWS. We are not a party to that data flow and do not receive copies.

## 5. Telemetry

The app sends no telemetry, analytics, crash reports, or usage data to us by default. If a future version offers optional diagnostics, it will be off by default, clearly labeled, and will exclude credentials and detection data.

## 6. No sale of data

We do not sell, rent, or share your personal data with anyone. We have no advertising partners and no data brokers, because by default we hold no data about you at all.

## 7. Your choices

To remove everything the app stores locally, uninstall the app and delete the stored credential entry from your operating system's credential store. Resources deployed into your AWS account remain yours; delete them from AWS if you no longer want them.

## 8. Contact

Privacy questions: [Contact Email].

---

# CONSENT-SCREEN SUMMARIES

**Terms of Use — short version:**
- Everything deploys into **your own AWS account**. You own the account, the data, and all AWS costs. AWS bills you directly; any cost figures we show are estimates.
- The app acts only on your instructions, using access keys you provide. Detection results are not guaranteed — accuracy depends on the training images you supply.
- The software is provided as is, with limited liability. Keep your AWS root account secured with MFA.

**Privacy Policy — short version:**
- Your AWS access keys are stored encrypted on this computer only, in the Windows credential store. They are never sent to us.
- Your card and AWS password go directly into AWS's own pages. We never see, store, or transmit them.
- No telemetry, no analytics, no data sold. Everything else travels directly between you and AWS over TLS.