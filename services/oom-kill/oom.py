import time


def allocate_memory(array : list):
    mem_size = 15
    byte_size = mem_size * 1024 * 1024
    allcocation = bytearray(byte_size)
    array.append(allcocation)

if __name__ == '__main__':
    l = list()
    i = 1
    while(True):
        allocate_memory(l)
        print(f"Iteration {i} allocation successful")
        print(f"Memory allocated : {i*15}Mi")
        i+=1
        time.sleep(10)
