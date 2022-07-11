import pdb
from src.springpython.config import PythonConfig, Object
from src.springpython.context import ApplicationContext


class WikiService(object):
    """
    serves wiki
    """

    def __init__(self):
        self.data = "wiki service"

    def get_name(self):
        return "test service"


class WikiProductAppConfig(PythonConfig):
    def __init__(self):
        super(WikiProductAppConfig, self).__init__()

    @Object
    def wiki_service(self):
        return WikiService()


if __name__ == "__main__":

    ctx = ApplicationContext(WikiProductAppConfig())
    service = ctx.get_object("wiki_service")

    assert service.get_name() == "test service"
