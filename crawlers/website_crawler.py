import logging
import os
from usp.tree import sitemap_tree_for_homepage
from core.crawler import Crawler, recursive_crawl
from core.utils import clean_urls, archive_extensions, img_extensions, get_file_extension, RateLimiter, setup_logging
from core.indexer import Indexer
import re
from typing import List, Set

import ray
import psutil

# disable USP annoying logging
logging.getLogger("usp.fetch_parse").setLevel(logging.ERROR)
logging.getLogger("usp.helpers").setLevel(logging.ERROR)


class PageCrawlWorker(object):
    def __init__(self, indexer: Indexer, crawler: Crawler, num_per_second: int):
        self.crawler = crawler
        self.indexer = indexer
        self.rate_limiter = RateLimiter(num_per_second)

    def setup(self):
        self.indexer.setup()
        setup_logging()

    def process(self, url: str, extraction: str, source: str):
        metadata = {"source": source, "url": url}
        if extraction == "pdf":
            try:
                with self.rate_limiter:
                    filename = self.crawler.url_to_file(url, title="")
            except Exception as e:
                logging.error(f"Error while processing {url}: {e}")
                return -1
            try:
                succeeded = self.indexer.index_file(filename, uri=url, metadata=metadata)
                if not succeeded:
                    logging.info(f"Indexing failed for {url}")
                else:
                    if os.path.exists(filename):
                        os.remove(filename)
                    logging.info(f"Indexing {url} was successful")
            except Exception as e:
                import traceback
                logging.error(
                    f"Error while indexing {url}: {e}, traceback={traceback.format_exc()}"
                )
                return -1
        else:  # use index_url which uses PlayWright
            logging.info(f"Crawling and indexing {url}")
            try:
                with self.rate_limiter:
                    succeeded = self.indexer.index_url(url, metadata=metadata)
                if not succeeded:
                    logging.info(f"Indexing failed for {url}")
                else:
                    logging.info(f"Indexing {url} was successful")
            except Exception as e:
                import traceback
                logging.error(
                    f"Error while indexing {url}: {e}, traceback={traceback.format_exc()}"
                )
                return -1
        return 0

class WebsiteCrawler(Crawler):
    def crawl(self) -> None:
        base_urls = self.cfg.website_crawler.urls
        self.pos_regex = [re.compile(r) for r in self.cfg.website_crawler.get("pos_regex", [])]
        self.neg_regex = [re.compile(r) for r in self.cfg.website_crawler.get("neg_regex", [])]

        # grab all URLs to crawl from all base_urls
        all_urls = []
        for homepage in base_urls:
            if self.cfg.website_crawler.pages_source == "sitemap":
                tree = sitemap_tree_for_homepage(homepage)
                urls = list(set([page.url for page in tree.all_pages()]))
            elif self.cfg.website_crawler.pages_source == "crawl":
                max_depth = self.cfg.website_crawler.get("max_depth", 3)
                urls_set = recursive_crawl(homepage, max_depth, 
                                           pos_regex=self.pos_regex, neg_regex=self.neg_regex, 
                                           indexer=self.indexer, visited=set(), verbose=self.indexer.verbose)
                urls = clean_urls(urls_set)
                urls = list(set(urls_set))
            else:
                logging.info(f"Unknown pages_source: {self.cfg.website_crawler.pages_source}")
                return
            logging.info(f"Found {len(urls)} URLs on {homepage}")
            all_urls += urls

        # remove URLS that are out of our regex regime or are archives or images
        urls = [u for u in all_urls if u.startswith('http') and not any([u.endswith(ext) for ext in archive_extensions + img_extensions])]
        if self.pos_regex and len(self.pos_regex)>0:
            urls = [u for u in all_urls if any([r.match(u) for r in self.pos_regex])]
        if self.neg_regex and len(self.neg_regex)>0:
            urls = [u for u in all_urls if not any([r.match(u) for r in self.neg_regex])]
        urls = list(set(urls))

        # crawl all URLs
        logging.info(f"Collected {len(urls)} URLs to crawl and index")

        # print some file types
        file_types = list(set([get_file_extension(u) for u in urls]))
        file_types = [t for t in file_types if t != ""]
        logging.info(f"File types = {file_types}")

        num_per_second = max(self.cfg.website_crawler.get("num_per_second", 10), 1)
        extraction = self.cfg.website_crawler.get("extraction", "playwright")   # "playwright" or "pdf"
        ray_workers = self.cfg.website_crawler.get("ray_workers", 0)            # -1: use ray with ALL cores, 0: dont use ray
        source = self.cfg.website_crawler.get("source", "website")

        if ray_workers == -1:
            ray_workers = psutil.cpu_count(logical=True)

        if ray_workers > 0:
            logging.info(f"Using {ray_workers} ray workers")
            self.indexer.p = self.indexer.browser = None
            ray.init(num_cpus=ray_workers, log_to_driver=True, include_dashboard=False)
            actors = [ray.remote(PageCrawlWorker).remote(self.indexer, self, num_per_second) for _ in range(ray_workers)]
            for a in actors:
                a.setup.remote()
            pool = ray.util.ActorPool(actors)
            _ = list(pool.map(lambda a, u: a.process.remote(u, extraction=extraction, source=source), urls))
                
        else:
            crawl_worker = PageCrawlWorker(self.indexer, self, num_per_second)
            for inx, url in enumerate(urls):
                if inx % 100 == 0:
                    logging.info(f"Crawling URL number {inx+1} out of {len(urls)}")
                crawl_worker.process(url, extraction=extraction, source=source)