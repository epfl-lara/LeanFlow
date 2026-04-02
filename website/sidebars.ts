import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: false,
      items: [
        'getting-started/start-here',
        'getting-started/quickstart',
        'getting-started/installation',
        'getting-started/updating',
        'getting-started/migration-to-gauss',
      ],
    },
    {
      type: 'category',
      label: 'Guides & Tutorials',
      collapsed: false,
      items: [
        'guides/tips',
      ],
    },
    {
      type: 'category',
      label: 'User Guide',
      collapsed: false,
      items: [
        'user-guide/cli',
        'user-guide/configuration',
        'user-guide/sessions',
        'user-guide/security',
      ],
    },
    {
      type: 'category',
      label: 'Developer Guide',
      items: [
        'developer-guide/architecture',
        'developer-guide/provider-runtime',
        'developer-guide/contributing',
      ],
    },
    {
      type: 'category',
      label: 'Reference',
      items: [
        'reference/slash-commands',
        'reference/environment-variables',
        'reference/faq',
      ],
    },
  ],
};

export default sidebars;
