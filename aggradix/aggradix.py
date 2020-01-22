import pylru
from .radix import RadixNode, RadixPrefix, RadixTree

def init_or_add(dic, key, val):
    if key in dic.keys():
        dic[key] += val
    else:
        dic[key] = val

def init_or_update(dic, key, val):
    if key in dic.keys():
        dic[key].update(val)
    else:
        dic[key] = val

class AggradixNode(RadixNode):
    n_node = 0

    def __init__(self, node_id=None):
        super(AggradixNode, self).__init__()
        if node_id is None:
            self.node_id = AggradixNode.n_node
            AggradixNode.n_node += 1
        else:
            self.node_id = node_id
        self.aggregated = False

    def set(self, prefix):
        self.prefix = prefix
        self.bitlen = prefix.bitlen
        self.free = False
        self.probed = {}
        self.respond = {}

    def reset(self):
        self.prefix = None
        self.left = None
        self.right = None
        self.free = True
        self.aggregated = False
        self.probed = {}
        self.respond = {}
    
    def __str__(self):
        return str(self.prefix)

class AggradixTree(RadixTree):
    def __init__(self, prefix, maxnode=64):
        super(AggradixTree, self).__init__()
        head = AggradixNode(node_id=-1)
        head.set(RadixPrefix(prefix))
        self.head = head
        self.maxnode = maxnode - 1
        self.free_nodes = self.maxnode
        self.packet_count = 0
        self.nodes = pylru.lrucache(self.maxnode)
        self.active_leaf_cache = self.nodes.head.prev
        self._lru_init()

    def _lru_init(self):
        for i in range(self.maxnode):
            self.nodes[i] = AggradixNode()

    def _lru_get_free(self):
        '''
        get free node
        Args:
        Returns:
            AggradixNode: free node
        '''
        entry = self.nodes.head.prev
        while entry is not None:
            if entry.empty or not entry.value.free:
                entry = entry.prev
                continue
            
            return entry.value

    def _lru_get_active(self):
        '''
        get Least Recent Used "active" node
        
        Args:
        Returns:
            AggradixNode: Least Recent Used "active" node
        '''
        entry = self.active_leaf_cache
        while entry != self.active_leaf_cache.next:
            if entry.empty or entry.value.free or entry.value.right is not None:
                entry = entry.prev
                continue

            self.active_leaf_cache = entry.prev
            return entry.value

    def _lru_move_tail(self, node):
        '''
        insert node to the tail of LRU cache.

        Args:
            node (AggradixNode): node to be inserted into the tail
        '''
        # move to the top
        self.nodes[node.node_id] = node
        
        # move to the tail
        entry = self.nodes.head
        self.nodes.head = entry.next

    def _common_prefix(self, node1, node2):
        '''
        Generate common prefix of node1 and node2.

        Args:
            node1 (RadixNode)
            node2 (RadixNode)

        Returns:
            RadixPrefix: common prefix of node1 and node2
        '''
        addr1 = node1.prefix.addr
        addr2 = node2.prefix.addr
        common_addr = bytearray(16)
        prefixmask = [0x00, 0x80, 0xc0, 0xe0, 0xf0, 0xf8, 0xfc, 0xfe]

        # differ_bit: addr1とaddr2の一致するビット長
        differ_bit = 0
        i = 0
        while i * 8 < 128:
            r = addr1[i] ^ addr2[i]
            if r == 0:
                differ_bit = (i + 1) * 8
                common_addr[i] = addr1[i]
                i += 1
                continue
            
            for j in range(8):
                if r & (0x80 >> j):
                    common_addr[i] = addr1[i] & prefixmask[j&7]
                    break
            differ_bit = i * 8 + j
            break

        return RadixPrefix(packed=common_addr, masklen=differ_bit)        

    def _subtree_sum(self, node):
        '''
        Args:
            sibling (AggradixNode): root node of subtree.
        Returns:
            int: sum of count of all nodes in subtree
        '''
        if node == None:
            return 0
        
        count = sum(node.probed.values())
        count += self._subtree_sum(node.left)
        count += self._subtree_sum(node.right)
        return count

    def _subtree_merge(self, node, is_root=False):
        if node is None:
            return {}, {}
        
        probed = node.probed
        respond = node.respond

        left_probed, left_respond = self._subtree_merge(node.left)
        for k, v in left_probed.items():
            init_or_add(probed, k, v)
        for k, v in left_respond.items():
            init_or_update(respond, k, v)

        right_probed, right_respond = self._subtree_merge(node.right)
        for k, v in right_probed.items():
            init_or_add(probed, k, v)
        for k, v in right_respond.items():
            init_or_update(respond, k, v)

        if is_root:
            node.probed = probed
            node.respond = respond

        else:
            if node.parent.left == node:
                node.parent.left = None
            else:
                node.parent.right = None
            self.free_nodes += 1

            node.reset()
            self._lru_move_tail(node)

        return probed, respond

    def _differ_bit(self, addr1, addr2, check_bit = 128):
        '''
        Args:
            addr1 (bytearray): address 1
            addr2 (bytearray): address 2
            checkbit (int): 何ビット目までチェックするか
        Returns:
            int: min(addr1とaddr2の一致するビット数, check_bit)
        '''
        differ_bit = 0
        i = 0
        while i * 8 < check_bit:
            r = addr1[i] ^ addr2[i]
            if r == 0:
                differ_bit = (i + 1) * 8
                i += 1
                continue
            for j in range(8):
                if r & (0x80 >> j):
                    break
            differ_bit = i * 8 + j
            break
        
        return check_bit if differ_bit > check_bit else differ_bit

    def add(self, prefix):
        '''
        1. add without using RadixGlue
        2. do not append node under aggregated node

        Args:
            prefix (RadixPrefix): prefix to be added
        '''
        addr = prefix.addr
        bitlen = prefix.bitlen

        # find proper position to insert given prefix.
        node = self.head
        while node.bitlen < bitlen:
            # right or left
            if (node.bitlen < self.maxbits and self._addr_test(addr, node.bitlen)):
                if node.right is None:
                    break
                node = node.right
            else:
                if node.left is None:
                    break
                node = node.left

        # differ_bit: how many bits differ between addr and test_addr
        test_addr = node.prefix.addr
        check_bit = node.bitlen if node.bitlen < bitlen else bitlen
        differ_bit = self._differ_bit(addr, test_addr, check_bit)
        
        parent = node.parent
        while parent and parent.bitlen >= differ_bit:
            node, parent = parent, node.parent

        if differ_bit == bitlen and node.bitlen ==bitlen:
            return node

        if node and node.aggregated:
            return node

        new_node = self._lru_get_free()
        new_node.set(prefix)
        self.free_nodes -= 1

        if node.bitlen == differ_bit:
            new_node.parent = node
            if (node.bitlen < self.maxbits and self._addr_test(addr, node.bitlen)):
                node.right = new_node
            else:
                node.left = new_node
            return new_node
        
        if bitlen == differ_bit:
            if bitlen < self.maxbits and self._addr_test(test_addr, bitlen):
                new_node.right = node
            else:
                new_node.left = node

            new_node.parent = node.parent

            if node.parent is None:
                self.head = new_node
            elif node.parent.right == node:
                node.parent.right = new_node
            else:
                node.parent.left = new_node
            
            node.parent = new_node

        else:
            glue_node = self._lru_get_free()
            glue_node.set(self._common_prefix(node, new_node))
            self.free_nodes -= 1            
            glue_node.parent = node.parent

            if differ_bit < self.maxbits and self._addr_test(addr, differ_bit):
                glue_node.right = new_node
                glue_node.left = node
            else:
                glue_node.right = node
                glue_node.left = new_node
        
            new_node.parent = glue_node
        
            if node.parent is None:
                self.head = glue_node
            elif node.parent.right == node:
                node.parent.right = glue_node
            else:
                node.parent.left = glue_node
        
            node.parent = glue_node
        
        return new_node            

    def search_covered(self, _prefix, head = None):
        if self.head is None:
            return None

        prefix = RadixPrefix(_prefix)
        if head is None:
            head = self.head

        # headのプレフィクス長が検索するプレフィクスのプレフィクス長より長い場合
        # 同じか短くなるまで木を上に探索
        while prefix.bitlen > head.prefix.bitlen:
            if head.parent is None:
                break
            head = head.parent

        results = []
        node = head
        addr = prefix.addr
        bitlen = prefix.bitlen

        while node.bitlen < bitlen:
            if self._addr_test(addr, node.bitlen):
                node = node.right
            else:
                node = node.left
            
            if node is None:
                return results

        stack = [node]
        while stack:
            node = stack.pop()
            if self._prefix_match(node.prefix, prefix, prefix.bitlen):
                results.append(node)
            if node.right:
                stack.append(node.right)
            if node.left:
                stack.append(node.left)
        
        return results

    def search_best(self, _prefix, head = None):
        if self.head is None:
            return None

        prefix = RadixPrefix(_prefix)
        if head is None:
            head = self.head
        
        node = head
        addr = prefix.addr
        bitlen = prefix.bitlen

        stack = []
        while node.bitlen < bitlen:
            stack.append(node)
            if self._addr_test(addr, node.bitlen):
                node = node.right
            else:
                node = node.left
            
            if node is None:
                break

        if node is not None:
            stack.append(node)
        
        if len(stack) <= 0:
            return None
        
        for node in stack[::-1]:
            if (self._prefix_match(node.prefix, prefix, node.bitlen)) and node.bitlen <= bitlen:
                return node
        return None

    def search_exact(self, _prefix, head = None):
        if self.head is None:
            return None

        prefix = RadixPrefix(_prefix)
        if head is None:
            head = self.head

        node = head
        addr = prefix.addr
        bitlen = prefix.bitlen

        while node.bitlen < bitlen:
            if self._addr_test(addr, node.bitlen):
                node = node.right
            else:
                node = node.left

            if node is None:
                return None

        if node.bitlen > bitlen:
            return None

        if self._prefix_match(node.prefix, prefix, bitlen):
            return node
        
        return None

    def search_worst(self, _prefix):
        return super().search_worst(RadixPrefix(_prefix))

    def aggregate(self):
        '''
        Aggregate Latest Recent Used Node.
        '''
        loopcount = 0
        while self.free_nodes < 2:
            thr = self.packet_count * 0.3 if self.packet_count > 10 else 10

            leaf = self._lru_get_active()
            
            if sum(leaf.probed.values()) > thr and loopcount < 10:
                self.nodes[leaf.node_id] = leaf
                loopcount += 1
                continue
            if leaf.parent == self.head:
                loopcount += 1
                continue
                
            parent = leaf.parent
            
            # 問答無用で集約する
            self._subtree_merge(parent, True)
            parent.aggregated = True

    def add_count(self, dst_addr, src_addr, respond = False):
        if self.free_nodes < 2:
            self.aggregate()
        
        dst_prefix = RadixPrefix(f'{dst_addr}/128')
        node = self.add(dst_prefix)

        # プローブを受けた回数を加算
        init_or_add(node.probed, src_addr, 1)

        # 応答を返した回数を加算
        if respond:
            if dst_addr not in node.respond.keys():
                node.respond[dst_addr] = {}
            if src_addr not in node.respond[dst_addr].keys():
                node.respond[dst_addr][src_addr] = 0
            node.respond[dst_addr][src_addr] += 1

    def cat_tree(self, head = None):
        print("**** aggradix dump ****")
        print(f'freenode: {self.free_nodes}\n')
        if head is None:
            head = self.head
        
        stack = [(head, 0)]
        while len(stack) > 0:
            node, depth = stack.pop()
            mark = " "
            if node.aggregated:
                mark = "*"

            print(f'{"-"*depth*2} {mark} {node.prefix}')
            # print(f'{" "*depth*2}     %%probed')
            # for d, s in node.probed.items():
            #     print(f'{" "*depth*2}     {d}: {s}')
            print(f'{" "*depth*2}     %%respond')
            for d, s in node.respond.items():
                print(f'{" "*depth*2}     {d}: {s}')

            if (node.left is not None):
                stack.append((node.left, depth+1))
            if (node.right is not None):
                stack.append((node.right, depth+1))
        print("**** dump fin ****\n")

if __name__ == "__main__":
    import ipaddress, random

    aggradix = AggradixTree("2001:db8::/48", maxnode=8)
    src_addresses = ["2001:db8:f::1", "2001:db8:f::1234"]
    
    base = ipaddress.ip_address("2001:db8::")
    dst_addrs = []
    for i in range(8):
        dst_addrs.append(base + random.randint(0, 2**(128-48)))
    
    for dst_addr in dst_addrs*3:
        import pdb; pdb.set_trace()
        aggradix.add_count(str(dst_addr), src_addresses[i%2], True)
        print(str(dst_addr))
        aggradix.cat_tree()
