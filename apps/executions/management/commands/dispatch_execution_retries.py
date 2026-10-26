from django.core.management.base import BaseCommand

from apps.executions.retry_outbox import consume_retry_outbox


class Command(BaseCommand):
    help = '消费 unified execution retry outbox；没有 durable dispatcher route 的记录会 fail-closed'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100, help='单次最多消费的 retry row 数量')
        parser.add_argument('--once', action='store_true', help='兼容 supervisor 调用；命令本身只执行一次')

    def handle(self, *args, **options):
        receipts = consume_retry_outbox(limit=max(int(options['limit']), 1))
        dispatched = sum(receipt.status == 'dispatched' for receipt in receipts)
        failed = sum(receipt.status == 'failed' for receipt in receipts)
        deferred = sum(receipt.status == 'deferred' for receipt in receipts)
        self.stdout.write(
            f'retry_outbox consumed={len(receipts)} dispatched={dispatched} '
            f'failed={failed} deferred={deferred}'
        )
        for receipt in receipts:
            if receipt.status == 'failed':
                self.stderr.write(
                    self.style.ERROR(
                        f'request={receipt.request_id} failed: {receipt.error}'
                    )
                )
