class PerfectPlayerLoss:
    def __init__(self, *a, **kw):
        self.conf_args = a
        self.conf_kw = kw
       
    def __call__(self, loss):
        def wrapper(*args, **kwargs):
            print('preprocessing')
            print('preprocessing configuration', self.conf_args, self.conf_kw)
            if args:
                if isinstance(args[0], int):
                    a = list(args)
                    a[0] += 5
                    args = tuple(a)
                    print('preprocess OK', args) 
            r = loss(*args, **kwargs)
            print('postprocessing', r)
            r += 7
            return r
        return wrapper
        
